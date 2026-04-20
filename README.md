# NIFTY Pro Tracker

A small 5-minute NIFTY 50 signal tracker for learning, paper-trading, and alerting.

It fetches a live NIFTY 50 snapshot from NSE, checks a simple live trend/breadth setup, and prints/sends a signal every 5 minutes during NSE market hours.

> This is not financial advice. Use it for education, backtesting, and paper trading before risking capital.

## Features

- NSE live NIFTY 50 snapshot
- Live price/open/previous-close trend check
- NIFTY 50 advance/decline breadth filter
- Range-based stop-loss and target levels
- Duplicate alert protection
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

## Run Every 5 Minutes

```bash
python nifty_pro_tracker.py
```

## Run From GitHub Actions

This repo includes a GitHub Actions workflow at `.github/workflows/nifty-signals.yml`.

It runs every 5 minutes on weekdays during the broad NSE window and also supports manual runs from the GitHub Actions tab. The Python script still checks NSE market hours in IST before sending alerts.

The tracker refuses to send `BUY` or `SELL` alerts when the latest candle is older than 15 minutes. This protects you from stale free-data responses.

To send Telegram alerts from GitHub:

1. Open your GitHub repo.
2. Go to `Settings` > `Secrets and variables` > `Actions`.
3. Add these repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Go to `Actions` > `NIFTY 5m Signals` > `Run workflow` to test it.

GitHub schedules are not guaranteed to fire at the exact second. They are good enough for lightweight alerting, but use a broker/VPS setup for serious live trading automation.

## Tune The Strategy

The default rules are intentionally simple:

- `BUY`: Live NIFTY is above open and previous close, advances are greater than declines, price change is at least 0.15%, and price is in the upper part of the intraday range.
- `SELL`: Live NIFTY is below open and previous close, declines are greater than advances, price change is at most -0.15%, and price is in the lower part of the intraday range.
- `WAIT`: Anything else.

You can edit these thresholds in `nifty_pro_tracker.py`.

## Data Note

The default source is NSE's live index snapshot endpoint. Official exchange-grade real-time tick feeds and 1/2/5-minute snapshot files are paid NSE Data & Analytics products. For serious live trading automation, use a broker API or licensed market data feed.
