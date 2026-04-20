from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv


IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "^NSEI"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

SignalSide = Literal["BUY", "SELL", "WAIT"]


@dataclass(frozen=True)
class Signal:
    side: SignalSide
    price: float
    time: datetime
    reason: str
    stop_loss: float | None = None
    target: float | None = None

    def alert_key(self) -> str:
        return f"{self.time.isoformat()}:{self.side}:{round(self.price, 2)}"


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False

    market_open = clock_time(9, 15)
    market_close = clock_time(15, 30)
    return market_open <= now.time() <= market_close


def fetch_candles(symbol: str = SYMBOL, interval: str = "5m", range_: str = "5d") -> pd.DataFrame:
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=requests.utils.quote(symbol, safe="")),
        params={"interval": interval, "range": range_},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    payload = response.json()

    chart = payload.get("chart", {})
    error = chart.get("error")
    if error:
        raise RuntimeError(error.get("description") or str(error))

    result = chart.get("result")
    if not result:
        raise RuntimeError("No chart result returned.")

    item = result[0]
    timestamps = item.get("timestamp", [])
    quote = item.get("indicators", {}).get("quote", [{}])[0]

    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(IST),
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
        }
    ).dropna()

    if frame.empty:
        raise RuntimeError("No usable candles returned.")

    return frame.reset_index(drop=True)


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    candles = frame.copy()

    candles["ema_9"] = candles["close"].ewm(span=9, adjust=False).mean()
    candles["ema_21"] = candles["close"].ewm(span=21, adjust=False).mean()

    delta = candles["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    candles["rsi"] = 100 - (100 / (1 + rs))

    ema_12 = candles["close"].ewm(span=12, adjust=False).mean()
    ema_26 = candles["close"].ewm(span=26, adjust=False).mean()
    candles["macd"] = ema_12 - ema_26
    candles["macd_signal"] = candles["macd"].ewm(span=9, adjust=False).mean()
    candles["macd_hist"] = candles["macd"] - candles["macd_signal"]

    previous_close = candles["close"].shift(1)
    true_range = pd.concat(
        [
            candles["high"] - candles["low"],
            (candles["high"] - previous_close).abs(),
            (candles["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    candles["atr"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()

    return candles


def build_signal(candles: pd.DataFrame) -> Signal:
    latest = candles.iloc[-1]
    price = float(latest["close"])
    atr = float(latest["atr"])
    candle_time = latest["time"].to_pydatetime()

    bullish = latest["ema_9"] > latest["ema_21"] and latest["macd_hist"] > 0 and 50 <= latest["rsi"] <= 72
    bearish = latest["ema_9"] < latest["ema_21"] and latest["macd_hist"] < 0 and 28 <= latest["rsi"] <= 50

    if bullish:
        return Signal(
            side="BUY",
            price=price,
            time=candle_time,
            reason=f"EMA trend up, MACD positive, RSI {latest['rsi']:.1f}",
            stop_loss=price - (1.2 * atr),
            target=price + (1.8 * atr),
        )

    if bearish:
        return Signal(
            side="SELL",
            price=price,
            time=candle_time,
            reason=f"EMA trend down, MACD negative, RSI {latest['rsi']:.1f}",
            stop_loss=price + (1.2 * atr),
            target=price - (1.8 * atr),
        )

    return Signal(
        side="WAIT",
        price=price,
        time=candle_time,
        reason=f"No clean setup. RSI {latest['rsi']:.1f}, MACD hist {latest['macd_hist']:.2f}",
    )


def format_signal(signal: Signal) -> str:
    lines = [
        f"NIFTY 5m Signal: {signal.side}",
        f"Time: {signal.time:%Y-%m-%d %H:%M %Z}",
        f"Price: {signal.price:.2f}",
        f"Reason: {signal.reason}",
    ]

    if signal.stop_loss is not None and signal.target is not None:
        lines.append(f"Stop-loss: {signal.stop_loss:.2f}")
        lines.append(f"Target: {signal.target:.2f}")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def seconds_until_next_5m(now: datetime | None = None) -> int:
    now = now or datetime.now(IST)
    next_run = now.replace(second=10, microsecond=0)
    minutes_to_add = 5 - (now.minute % 5)
    next_run += timedelta(minutes=minutes_to_add)

    delta = (next_run - now).total_seconds()
    return max(30, int(delta))


def run_cycle() -> Signal:
    candles = add_indicators(fetch_candles())
    signal = build_signal(candles)
    message = format_signal(signal)
    print(message)
    print()
    return signal


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY 50 5-minute signal tracker")
    parser.add_argument("--once", action="store_true", help="Run a single signal check and exit.")
    parser.add_argument("--send-wait-alerts", action="store_true", help="Also send Telegram alerts for WAIT signals.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Run even outside NSE market hours.")
    args = parser.parse_args()

    load_dotenv()
    last_alert_key: str | None = None

    while True:
        try:
            if args.ignore_market_hours or is_market_open():
                signal = run_cycle()
                if signal.alert_key() == last_alert_key:
                    print("Duplicate candle detected; alert already handled.")
                elif signal.side != "WAIT" or args.send_wait_alerts:
                    send_telegram(format_signal(signal))
                last_alert_key = signal.alert_key()
            else:
                print(f"Market closed at {datetime.now(IST):%Y-%m-%d %H:%M %Z}. Waiting...")

            if args.once:
                break

        except Exception as exc:
            print(f"Error: {exc}")
            if args.once:
                raise

        time.sleep(seconds_until_next_5m())


if __name__ == "__main__":
    main()
