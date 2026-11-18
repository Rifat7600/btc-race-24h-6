[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/Rifat7600/btc-race-24h-6/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/Rifat7600/btc-race-24h-6/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

# RSI-Bounce BTC Bot

A trading bot for the [make.fun "Race a BTC trading bot live for 24 hours"](https://make.fun/bounty/btc-race-24h-20261118-1444) bounty. It trades Coinbase Wrapped BTC (CBBTC) against USDC, one minute at a time, in the `agent-fun/bars-replay/v1` replay (newline-delimited JSON on stdin/stdout).

## Idea

One-minute BTC is mostly noise, so a plain oversold bounce loses to fees. The money is in sharp washouts: a steep 15-minute plunge that leaves the 60-bar RSI deeply oversold, followed by a multi-hour recovery. This bot waits for exactly that setup, buys it in full, and lets the recovery run until either a profit target is hit or the medium-term (15-minute) RSI rolls over from overbought.

- **Entry** (on a bar's close): 60-bar RSI of 1-minute closes below 30, the last 15 closes fell at least 200 bps, and the bot is re-armed (after any exit, the 60-bar RSI must first recover above 50).
- **Exit** (first of these): close 250 bps above the average fill price; 15-minute RSI peaked at or above 65 and has fallen below 60; trailing stop 250 bps below the peak close; hard stop 300 bps below the average fill price.

## Dev-window result

Backtested on the kit's published 24-hour dev window (1,440 one-minute bars) through a local replay of the exact protocol and fill model (4 bps fee, 2 bps slippage, constant-product impact):

- **Net PnL: +23.53 USDC** on 1,000 USDC starting capital
- Trades: 2 (one buy, one sell)
- Maximum drawdown: 1.50%

Everything is deterministic: two runs over the same bars produce byte-identical replies. No clock, no randomness, no network, no files at runtime.

## Run

```sh
python3 -P -s main.py
```

The bot reads `hello` / `batch` / `done` JSON lines on stdin and writes one `reply` JSON line per batch on stdout. Python 3.13, standard library only.
