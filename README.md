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

## Tune The Strategy

The default rules are intentionally simple:

- `BUY`: EMA 9 is above EMA 21, MACD histogram is positive, and RSI is between 50 and 72.
- `SELL`: EMA 9 is below EMA 21, MACD histogram is negative, and RSI is between 28 and 50.
- `WAIT`: Anything else.

You can edit these thresholds in `nifty_pro_tracker.py`.

## Data Note

Free sources can be delayed, rate-limited, or incomplete. For live trading, use a broker API or an official licensed market data feed.
