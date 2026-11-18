"""RSI-bounce bot for the make.fun BTC race (bars-replay/v1).

Idea: 1-minute BTC is mostly noise, so a plain oversold bounce loses to
fees. The money is in sharp washouts: a steep 15-minute plunge that
leaves the 60-bar RSI deeply oversold, followed by a multi-hour
recovery. This bot waits for exactly that setup, buys it in full, and
lets the recovery run until either a profit target is hit or the
medium-term (15-minute) RSI rolls over from overbought.

Entry (all must hold, on a bar's close):
  - 60-bar RSI of 1-minute closes below 30 (deeply oversold)
  - the last 15 closes fell at least 200 bps (a washout, not a grind)
  - re-armed: after any exit, the 60-bar RSI must first recover above 50
Exit (first of these that fires, on a bar's close):
  - profit target: close 250 bps above the average fill price
  - rollover: 15-minute RSI peaked at or above 65 while in the trade
    and has since fallen below 60
  - trailing stop: close 250 bps below the highest close since entry
  - hard stop: close 300 bps below the average fill price

Everything is deterministic: decisions come only from the bars and the
hello message. No clock, no randomness, no network, no files.
"""

import json
import sys

# ---- tuned constants (from the dev-window backtest) -----------------------
RSI_BARS = 60          # lookback of the entry oscillator, in 1-minute bars
RSI_OVERSOLD = 30.0    # entry: RSI below this
RSI_REARM = 50.0       # re-arm after an exit: RSI must recover above this
WASH_BARS = 15         # washout window, in bars
WASH_DROP_BPS = 200    # washout: 15-bar return at or below -200 bps
BUCKET_MINUTES = 15    # aggregation for the medium-term oscillator
ROLL_RSI_BARS = 14     # lookback of the medium-term RSI, in buckets
ROLL_PEAK = 65.0       # rollover exit: medium RSI reached at least this
ROLL_EXIT = 60.0       # rollover exit: medium RSI has fallen below this
TARGET_BPS = 250       # profit target, in bps above average fill price
TRAIL_BPS = 250        # trailing stop, in bps below the peak close
STOP_BPS = 300         # hard stop, in bps below the average fill price
MAX_SLIPPAGE_BPS = 100  # per-order slippage guard vs the latest close
BPS = 10000


def wilder_rsi(closes, period):
    """Wilder RSI of a list of closes. Returns None until `period` closes
    after the first one have been seen. Matches the standard definition:
    the first value seeds from the simple average of the first `period`
    changes, then Wilder smoothing takes over."""
    n = len(closes)
    if n <= period:
        return None
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains[i] = diff
        else:
            losses[i] = -diff
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


class SymbolBook:
    """Per-symbol price history and trade state."""

    def __init__(self):
        self.closes = []          # 1-minute closes, as floats
        self.bucket_span_ms = BUCKET_MINUTES * 60 * 1000
        self.bucket_id = None     # bucket of the bar being accumulated
        self.bucket_close = None  # last close seen in the open bucket
        self.done_buckets = []    # closes of fully completed buckets
        self.armed = True         # may enter (re-arm rule)
        self.avg_price = 0.0      # average fill price of the open position
        self.peak_close = 0.0     # highest close since entry
        self.peak_mid_rsi = 0.0   # highest medium-term RSI since entry
        self.sell_pending = False  # a sell order is out, awaiting its fill

    def note_bar(self, bar):
        """Record a bar's close and finalize any bucket it completes."""
        close = int(bar["c"])
        self.closes.append(close)
        bucket = int(bar["t"]) // self.bucket_span_ms
        if self.bucket_id is None:
            self.bucket_id = bucket
            self.bucket_close = close
        elif bucket == self.bucket_id:
            self.bucket_close = close
        else:
            # The old bucket is complete: its close joins the history.
            self.done_buckets.append(self.bucket_close)
            self.bucket_id = bucket
            self.bucket_close = close

    def fast_rsi(self):
        return wilder_rsi(self.closes, RSI_BARS)

    def mid_rsi(self):
        return wilder_rsi(self.done_buckets, ROLL_RSI_BARS)

    def washout_drop_bps(self):
        """Return of the last WASH_BARS closes, in bps, or None."""
        if len(self.closes) <= WASH_BARS:
            return None
        now = self.closes[-1]
        then = self.closes[-1 - WASH_BARS]
        return (now - then) / then * BPS


class RsiBounceBot:
    def __init__(self, hello):
        self.symbols = [s["symbol"] for s in hello["symbols"]]
        self.scales = {s["symbol"]: (s["price_scale"], s["qty_scale"])
                       for s in hello["symbols"]}
        self.quote_scale = hello["capital"]["quote_scale"]
        self.cash_start = int(hello["capital"]["quote_units"])
        self.max_positions = hello["caps"]["max_open_positions"]
        self.max_weight_bps = hello["caps"]["max_weight_bps"]
        self.max_orders = hello["caps"]["max_orders_per_bar"]
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.warmup = hello.get("warmup_bars", 0)
        self.books = {sym: SymbolBook() for sym in self.symbols}
        self.bars_seen = 0
        self.order_no = 0

    def _next_id(self, tag):
        self.order_no += 1
        return "%s-%d" % (tag, self.order_no)

    def handle_batch(self, msg):
        seq = msg["seq"]
        account = msg["account"]
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        held = {p["symbol"]: int(p["qty"]) for p in account["positions"]}
        self.bars_seen += 1

        # 1. Learn from this batch's fills (authoritative fill prices).
        # The account in this batch already includes these fills, so the
        # pre-fill quantity is the held quantity minus the filled one.
        for fill in msg["fills"]:
            book = self.books.get(fill["symbol"])
            if book is None:
                continue
            if fill["status"] == "rejected":
                if fill["side"] == "sell":
                    book.sell_pending = False
                continue
            if fill["status"] != "filled":
                continue
            price = int(fill["price"])
            qty = int(fill["qty"])
            held_qty = held.get(fill["symbol"], 0)
            if fill["side"] == "buy":
                if held_qty <= qty or book.avg_price == 0.0:
                    book.avg_price = float(price)
                    book.peak_close = float(price)
                    book.peak_mid_rsi = 0.0
                else:
                    prev_qty = held_qty - qty
                    book.avg_price = (book.avg_price * prev_qty
                                      + price * qty) / held_qty
            elif fill["side"] == "sell":
                book.sell_pending = False
                if held_qty == 0:
                    book.armed = False
                    book.avg_price = 0.0
                    book.peak_close = 0.0
                    book.peak_mid_rsi = 0.0

        # 2. Record the new bars.
        for bar in msg["bars"]:
            book = self.books.get(bar["symbol"])
            if book is not None:
                book.note_bar(bar)

        orders = []
        if self.bars_seen <= self.warmup:
            return {"type": "reply", "seq": seq, "orders": orders}

        open_count = sum(1 for q in held.values() if q > 0)
        for symbol in self.symbols:
            if len(orders) >= self.max_orders:
                break
            book = self.books[symbol]
            if len(book.closes) < 2:
                continue
            close = book.closes[-1]
            qty = held.get(symbol, 0)
            fast = book.fast_rsi()
            mid = book.mid_rsi()

            if qty == 0:
                # Re-arm: after an exit, wait for the oscillator to heal.
                if fast is not None and fast >= RSI_REARM:
                    book.armed = True
                if not book.armed:
                    continue
                if open_count >= self.max_positions:
                    continue
                if fast is None or fast >= RSI_OVERSOLD:
                    continue
                drop = book.washout_drop_bps()
                if drop is None or drop > -WASH_DROP_BPS:
                    continue
                notional = cash
                weight_cap = equity * self.max_weight_bps // BPS
                if notional > weight_cap:
                    notional = weight_cap
                if notional < self.min_notional:
                    continue
                orders.append({
                    "side": "buy",
                    "client_id": self._next_id("b"),
                    "symbol": symbol,
                    "notional_quote_units": str(notional),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                })
                open_count += 1
                cash -= notional
            else:
                # In a position: maintain the running peak and exit checks.
                if close > book.peak_close:
                    book.peak_close = float(close)
                if mid is not None and mid > book.peak_mid_rsi:
                    book.peak_mid_rsi = mid
                if book.sell_pending:
                    continue
                leave = False
                if book.avg_price > 0:
                    gain_bps = (close - book.avg_price) / book.avg_price * BPS
                    if gain_bps >= TARGET_BPS:
                        leave = True
                    if (book.avg_price - close) / book.avg_price * BPS >= STOP_BPS:
                        leave = True
                if (book.peak_close > 0
                        and (book.peak_close - close) / book.peak_close * BPS >= TRAIL_BPS):
                    leave = True
                if (book.peak_mid_rsi >= ROLL_PEAK and mid is not None
                        and mid < ROLL_EXIT):
                    leave = True
                if leave:
                    orders.append({
                        "side": "sell",
                        "client_id": self._next_id("s"),
                        "symbol": symbol,
                        "qty": str(qty),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    })
                    book.sell_pending = True
        return {"type": "reply", "seq": seq, "orders": orders}


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot = RsiBounceBot(msg)
        elif kind == "batch":
            if bot is None:
                continue
            reply = bot.handle_batch(msg)
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
