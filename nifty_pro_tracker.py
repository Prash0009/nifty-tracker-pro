# from __future__ import annotations

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
NSE_HOME_URL = "https://www.nseindia.com/market-data/live-market-indices"
NSE_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NSE_INDEX_NAME = "NIFTY 50"
LIVE_INDEX_CONFIG = {
    "NIFTY 50": {"display": "NIFTY", "strike_step": 50},
    "NIFTY BANK": {"display": "BANKNIFTY", "strike_step": 100},
}

SignalSide = Literal["BUY", "SELL", "WAIT"]


@dataclass(frozen=True)
class Signal:
    side: SignalSide
    price: float
    time: datetime
    reason: str
    instrument: str = "NIFTY"
    option_type: str | None = None
    strike: int | None = None
    score: float = 0
    stop_loss: float | None = None
    target: float | None = None
    details: tuple["Signal", ...] = ()

    def alert_key(self) -> str:
        return (
            f"{self.time.isoformat()}:{self.instrument}:{self.side}:"
            f"{self.option_type or 'NA'}:{self.strike or 'NA'}:{round(self.price, 2)}"
        )


@dataclass(frozen=True)
class LiveIndexSnapshot:
    index: str
    last: float
    open: float
    high: float
    low: float
    previous_close: float
    percent_change: float
    advances: int
    declines: int
    timestamp: datetime


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False

    market_open = clock_time(9, 15)
    market_close = clock_time(15, 30)
    return market_open <= now.time() <= market_close


def fetch_nse_live_snapshots(index_names: tuple[str, ...]) -> list[LiveIndexSnapshot]:
    session = requests.Session()
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME_URL,
        "User-Agent": "Mozilla/5.0",
    }
    session.get(NSE_HOME_URL, headers=headers, timeout=20)
    response = session.get(NSE_INDICES_URL, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json()

    timestamp = datetime.strptime(payload["timestamp"], "%d-%b-%Y %H:%M").replace(tzinfo=IST)
    rows = payload.get("data", [])
    snapshots: list[LiveIndexSnapshot] = []
    missing: list[str] = []

    for index_name in index_names:
        row = next((item for item in rows if item.get("index") == index_name), None)
        if not row:
            missing.append(index_name)
            continue

        snapshots.append(
            LiveIndexSnapshot(
                index=row["index"],
                last=float(row["last"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                previous_close=float(row["previousClose"]),
                percent_change=float(row["percentChange"]),
                advances=int(row.get("advances") or 0),
                declines=int(row.get("declines") or 0),
                timestamp=timestamp,
            )
        )

    if missing:
        raise RuntimeError(f"Missing NSE live indices data for: {', '.join(missing)}")

    return snapshots


def fetch_nse_live_snapshot(index_name: str = NSE_INDEX_NAME) -> LiveIndexSnapshot:
    return fetch_nse_live_snapshots((index_name,))[0]


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


def nearest_strike(price: float, strike_step: int) -> int:
    return int(round(price / strike_step) * strike_step)


def build_live_signal(snapshot: LiveIndexSnapshot, max_candle_age_minutes: int = 15) -> Signal:
    config = LIVE_INDEX_CONFIG.get(snapshot.index, {"display": snapshot.index, "strike_step": 50})
    instrument = config["display"]
    strike = nearest_strike(snapshot.last, int(config["strike_step"]))
    data_age = datetime.now(IST) - snapshot.timestamp
    if data_age > timedelta(minutes=max_candle_age_minutes):
        return Signal(
            side="WAIT",
            price=snapshot.last,
            time=snapshot.timestamp,
            instrument=instrument,
            reason=(
                f"Stale NSE live data: latest update is {data_age} old. "
                f"Max allowed age is {max_candle_age_minutes} minutes."
            ),
        )

    intraday_range = max(snapshot.high - snapshot.low, 1.0)
    close_location = (snapshot.last - snapshot.low) / intraday_range
    risk_unit = max(intraday_range * 0.35, abs(snapshot.last - snapshot.previous_close) * 0.5, 10.0)
    breadth_total = max(snapshot.advances + snapshot.declines, 1)
    breadth_strength = abs(snapshot.advances - snapshot.declines) / breadth_total
    trend_strength = abs(snapshot.last - snapshot.open) / intraday_range
    momentum_strength = abs(snapshot.percent_change)
    score = (momentum_strength * 100) + (breadth_strength * 25) + (trend_strength * 25)

    bullish = (
        snapshot.last > snapshot.open
        and snapshot.last > snapshot.previous_close
        and snapshot.advances > snapshot.declines
        and snapshot.percent_change >= 0.15
        and close_location >= 0.60
    )
    bearish = (
        snapshot.last < snapshot.open
        and snapshot.last < snapshot.previous_close
        and snapshot.declines > snapshot.advances
        and snapshot.percent_change <= -0.15
        and close_location <= 0.40
    )

    breadth = f"advances {snapshot.advances}, declines {snapshot.declines}"
    if bullish:
        return Signal(
            side="BUY",
            price=snapshot.last,
            time=snapshot.timestamp,
            instrument=instrument,
            option_type="CE",
            strike=strike,
            score=score,
            reason=(
                f"NSE live trend up: buy ATM {instrument} {strike} CE. "
                f"Price above open/previous close, "
                f"{breadth}, change {snapshot.percent_change:.2f}%"
            ),
            stop_loss=snapshot.last - risk_unit,
            target=snapshot.last + (1.5 * risk_unit),
        )

    if bearish:
        return Signal(
            side="BUY",
            price=snapshot.last,
            time=snapshot.timestamp,
            instrument=instrument,
            option_type="PE",
            strike=strike,
            score=score,
            reason=(
                f"NSE live trend down: buy ATM {instrument} {strike} PE. "
                f"Price below open/previous close, "
                f"{breadth}, change {snapshot.percent_change:.2f}%"
            ),
            stop_loss=snapshot.last + risk_unit,
            target=snapshot.last - (1.5 * risk_unit),
        )

    return Signal(
        side="WAIT",
        price=snapshot.last,
        time=snapshot.timestamp,
        instrument=instrument,
        strike=strike,
        score=score,
        reason=(
            f"No clean NSE live setup. Open {snapshot.open:.2f}, high {snapshot.high:.2f}, "
            f"low {snapshot.low:.2f}, prev close {snapshot.previous_close:.2f}, "
            f"{breadth}, change {snapshot.percent_change:.2f}%"
        ),
    )


def build_best_live_option_signal(max_candle_age_minutes: int = 15) -> Signal:
    snapshots = fetch_nse_live_snapshots(tuple(LIVE_INDEX_CONFIG.keys()))
    signals = [
        build_live_signal(snapshot, max_candle_age_minutes=max_candle_age_minutes)
        for snapshot in snapshots
    ]
    actionable = [signal for signal in signals if signal.side == "BUY" and signal.option_type]

    if actionable:
        recommended = max(actionable, key=lambda signal: signal.score)
        return Signal(
            side=recommended.side,
            price=recommended.price,
            time=recommended.time,
            reason=recommended.reason,
            instrument=recommended.instrument,
            option_type=recommended.option_type,
            strike=recommended.strike,
            score=recommended.score,
            stop_loss=recommended.stop_loss,
            target=recommended.target,
            details=tuple(signals),
        )

    return Signal(
        side="WAIT",
        price=signals[0].price,
        time=max(signal.time for signal in signals),
        instrument="NIFTY/BANKNIFTY",
        score=max(signal.score for signal in signals),
        reason="No clean option-buy setup. " + " | ".join(
            f"{signal.instrument}: {signal.reason}" for signal in signals
        ),
        details=tuple(signals),
    )


def build_signal(candles: pd.DataFrame, max_candle_age_minutes: int = 15) -> Signal:
    latest = candles.iloc[-1]
    price = float(latest["close"])
    atr = float(latest["atr"])
    candle_time = latest["time"].to_pydatetime()
    candle_age = datetime.now(IST) - candle_time

    if candle_age > timedelta(minutes=max_candle_age_minutes):
        return Signal(
            side="WAIT",
            price=price,
            time=candle_time,
            reason=(
                f"Stale data: latest candle is {candle_age} old. "
                f"Max allowed age is {max_candle_age_minutes} minutes."
            ),
        )

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
    title = "NIFTY/BANKNIFTY 5m Option Signal"
    if signal.option_type and signal.strike:
        title = f"{signal.instrument} {signal.strike} {signal.option_type} Signal"

    lines = [
        f"{title}: {signal.side}",
        f"Time: {signal.time:%Y-%m-%d %H:%M %Z}",
        f"Underlying: {signal.instrument}",
        f"Spot price: {signal.price:.2f}",
        f"Reason: {signal.reason}",
    ]

    if signal.option_type and signal.strike:
        lines.insert(3, f"Option idea: BUY {signal.instrument} {signal.strike} {signal.option_type}")

    if signal.stop_loss is not None and signal.target is not None:
        lines.append(f"Underlying stop-loss: {signal.stop_loss:.2f}")
        lines.append(f"Underlying target: {signal.target:.2f}")

    if signal.details:
        lines.append("")
        lines.append("Both index checks:")
        for detail in signal.details:
            if detail.option_type and detail.strike:
                status = f"BUY {detail.instrument} {detail.strike} {detail.option_type}"
            else:
                status = "WAIT"

            marker = "Recommended" if (
                detail.instrument == signal.instrument
                and detail.option_type == signal.option_type
                and detail.strike == signal.strike
            ) else "Also checked"
            lines.append(f"- {marker}: {detail.instrument}: {status}")
            lines.append(f"  Spot {detail.price:.2f}. {detail.reason}")

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


def run_cycle(source: str = "nse-live", max_candle_age_minutes: int = 15) -> Signal:
    if source == "nse-live":
        signal = build_best_live_option_signal(max_candle_age_minutes=max_candle_age_minutes)
        message = format_signal(signal)
        print(message)
        print()
        return signal

    candles = add_indicators(fetch_candles())
    signal = build_signal(candles, max_candle_age_minutes=max_candle_age_minutes)
    message = format_signal(signal)
    print(message)
    print()
    return signal


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY 50 5-minute signal tracker")
    parser.add_argument("--once", action="store_true", help="Run a single signal check and exit.")
    parser.add_argument("--send-wait-alerts", action="store_true", help="Also send Telegram alerts for WAIT signals.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Run even outside NSE market hours.")
    parser.add_argument(
        "--source",
        choices=("nse-live", "yahoo"),
        default="nse-live",
        help="Market data source. nse-live uses NSE's live index snapshot endpoint.",
    )
    parser.add_argument(
        "--max-candle-age-minutes",
        type=int,
        default=15,
        help="Do not produce BUY/SELL alerts when the latest candle is older than this many minutes.",
    )
    args = parser.parse_args()

    load_dotenv()
    last_alert_key: str | None = None

    while True:
        try:
            if args.ignore_market_hours or is_market_open():
                signal = run_cycle(source=args.source, max_candle_age_minutes=args.max_candle_age_minutes)
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
