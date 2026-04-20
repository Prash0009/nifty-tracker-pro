# NIFTY Pro Tracker

A small 5-minute NIFTY 50 signal tracker for learning, paper-trading, and alerting.

It fetches intraday candles for `^NSEI`, calculates trend and momentum indicators, and prints/sends a signal every 5 minutes during NSE market hours.

> This is not financial advice. Use it for education, backtesting, and paper trading before risking capital.

## Features

- 5-minute NIFTY 50 candles
- EMA 9 / EMA 21 trend signal
- RSI momentum filter
- MACD confirmation
- ATR-based stop-loss and target levels
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

- `BUY`: EMA 9 is above EMA 21, MACD histogram is positive, and RSI is between 50 and 72.
- `SELL`: EMA 9 is below EMA 21, MACD histogram is negative, and RSI is between 28 and 50.
- `WAIT`: Anything else.

You can edit these thresholds in `nifty_pro_tracker.py`.

## Data Note

Free sources can be delayed, rate-limited, or incomplete. For live trading, use a broker API or an official licensed market data feed.
