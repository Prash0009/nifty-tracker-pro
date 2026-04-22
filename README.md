# NIFTY Pro Tracker

A stateful NIFTY-only option signal tracker for learning, paper-trading, and alerting.

It fetches live NIFTY 50 data from NSE, maintains 5-minute signal state, sends `BUY`, `HOLD`, `SELL`, and `WAIT` updates to Telegram, and includes a backtest mode that uses the same scoring rules.

> This is not financial advice. Use it for education, backtesting, and paper trading before risking capital.

## Features

- NIFTY-only live tracking
- 5-minute candle-based signal logic
- Exact entry on confirmed candle close
- Tighter weighted scoring with EMA, RSI, MACD, breadth, candle-body, and breakout filters
- ATM option idea: `NIFTY CE` or `NIFTY PE`
- Stateful `HOLD` and `SELL` tracking
- Immediate alert when the watch state changes
- Hourly Telegram summary
- Backtest mode with win/loss tables and drawdown
- Stale-data protection
- Optional Telegram alerts
- NSE market-hour guard

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

If you want Telegram alerts, edit `.env`:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## Run Once

```bash
python nifty_pro_tracker.py --once
```

## Run Continuously Every 2 Minutes

```bash
python nifty_pro_tracker.py --loop --poll-seconds 120
```

This is the better mode for a VPS or cloud worker because it gives you:

- immediate `BUY` and `SELL` alerts
- `HOLD` updates every 2 minutes while a trade is active
- immediate alert when the signal watch flips between bullish, bearish, and neutral
- hourly status messages even when there is no new trade

## Run Backtest

```bash
python nifty_pro_tracker.py --backtest --backtest-range 30d
```

## Run From GitHub Actions

This repo includes a GitHub Actions workflow at `.github/workflows/nifty-signals.yml`.

It runs every 5 minutes on weekdays during the broad NSE window and also supports manual runs from the GitHub Actions tab. The workflow commits `.tracker_state.json` back to the repo so the live trade state survives between runs.

The tracker refuses to send option alerts when the latest snapshot is older than 15 minutes. This protects you from stale free-data responses.

To send Telegram alerts from GitHub:

1. Open your GitHub repo.
2. Go to `Settings` > `Secrets and variables` > `Actions`.
3. Add these repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Go to `Actions` > `NIFTY 5m Option Signals` > `Run workflow` to test it.

GitHub Actions schedules are not guaranteed to fire at the exact second, and the shortest supported interval is once every 5 minutes. That means true 2-minute Telegram `HOLD` updates are not possible on GitHub-hosted schedules alone. The code supports 2-minute monitoring, but for actual 2-minute delivery you should run the tracker in `--loop` mode on a VPS, cloud worker, or broker-hosted automation.

## Better Trigger Plan

For this strategy, the cleanest setup is:

1. Run the tracker continuously every 2 minutes on a cloud worker using `--loop --poll-seconds 120`.
2. Keep `BUY`, `SELL`, and signal-watch changes as immediate Telegram alerts.
3. Keep `HOLD` updates every 2 minutes only while a trade is active.
4. Keep an hourly Telegram status no matter what the current state is.

If you must stay on GitHub-only hosting, use the workflow as a 5-minute fallback. That is still acceptable for a candle-close entry strategy, because entries are only taken after a confirmed 5-minute candle close anyway.

## Tune The Strategy

The live engine is NIFTY-only and uses candle-close confirmation:

- `BUY CE`: EMA 9 is above EMA 21, MACD histogram is positive and improving, RSI is healthy, breadth/session structure is supportive, candle body is strong, and the latest candle breaks above recent highs with enough score.
- `BUY PE`: EMA 9 is below EMA 21, MACD histogram is negative and weakening further, RSI is weak, breadth/session structure is supportive, candle body is strong, and the latest candle breaks below recent lows with enough score.
- `HOLD`: An existing active trade is still valid and stop/target has not been hit.
- `SELL`: An active trade hits stop-loss or target.
- `WAIT`: No new valid entry.

The tracker rounds to the nearest ATM strike in 50-point NIFTY steps. You can edit the thresholds and scoring in `nifty_pro_tracker.py`.

## Data Note

The live source is NSE's live index snapshot endpoint, while backtesting uses Yahoo Finance historical candles. Official exchange-grade real-time tick feeds and 1/2/5-minute snapshot files are paid NSE Data & Analytics products. For serious live trading automation, use a broker API or licensed market data feed.
