from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv


IST = ZoneInfo("Asia/Kolkata")
NSE_HOME_URL = "https://www.nseindia.com/market-data/live-market-indices"
NSE_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NIFTY_INDEX_NAME = "NIFTY 50"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SYMBOL = "^NSEI"
STATE_PATH = Path(".tracker_state.json")
DEFAULT_HOLD_INTERVAL_MINUTES = 2
DEFAULT_HOURLY_INTERVAL_MINUTES = 60
NIFTY_STRIKE_STEP = 50

RunStatus = Literal["BUY", "SELL", "HOLD", "WAIT"]
TradeSide = Literal["CE", "PE"]


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


@dataclass(frozen=True)
class Trade:
    option_type: TradeSide
    strike: int
    entry_price: float
    entry_time: datetime
    stop_loss: float
    target: float
    score: float
    status: str = "OPEN"


@dataclass(frozen=True)
class TrackerResult:
    status: RunStatus
    title: str
    spot_price: float
    time: datetime
    reason: str
    option_type: str | None = None
    strike: int | None = None
    score: float = 0
    stop_loss: float | None = None
    target: float | None = None
    pnl_points: float | None = None
    signal_state: str | None = None
    should_alert: bool = False
    hourly_summary: bool = False
    signal_changed: bool = False

    def alert_key(self) -> str:
        return (
            f"{self.status}:{self.option_type or 'NA'}:{self.strike or 'NA'}:"
            f"{self.signal_state or 'NA'}"
        )


def now_ist() -> datetime:
    return datetime.now(IST)


def floor_to_5m(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def round_to_strike(price: float) -> int:
    return int(round(price / NIFTY_STRIKE_STEP) * NIFTY_STRIKE_STEP)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def default_state() -> dict:
    return {
        "candles": [],
        "active_trade": None,
        "last_hold_alert_at": None,
        "last_hourly_alert_at": None,
        "last_signal_bucket": None,
        "last_signal_signature": None,
    }


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return default_state()
    return {**default_state(), **json.loads(path.read_text())}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def is_market_open(current: datetime | None = None) -> bool:
    current = current or now_ist()
    if current.weekday() >= 5:
        return False
    return clock_time(9, 15) <= current.time() <= clock_time(15, 30)


def fetch_nse_live_snapshot(index_name: str = NIFTY_INDEX_NAME) -> LiveIndexSnapshot:
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

    row = next((item for item in payload.get("data", []) if item.get("index") == index_name), None)
    if not row:
        raise RuntimeError(f"{index_name} not found in NSE live data.")

    timestamp = datetime.strptime(payload["timestamp"], "%d-%b-%Y %H:%M").replace(tzinfo=IST)
    return LiveIndexSnapshot(
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


def fetch_yahoo_candles(interval: str = "5m", range_: str = "30d") -> pd.DataFrame:
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=requests.utils.quote(YAHOO_SYMBOL, safe="")),
        params={"interval": interval, "range": range_},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("chart", {}).get("result")
    if not result:
        raise RuntimeError("No Yahoo candle result returned.")

    item = result[0]
    quote = item["indicators"]["quote"][0]
    frame = pd.DataFrame(
        {
            "time": pd.to_datetime(item["timestamp"], unit="s", utc=True).tz_convert(IST),
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "close": quote["close"],
        }
    ).dropna()
    return frame.reset_index(drop=True)


def append_snapshot_candle(state: dict, snapshot: LiveIndexSnapshot) -> None:
    bucket = floor_to_5m(snapshot.timestamp)
    candles = state["candles"]
    candle = {
        "bucket": bucket.isoformat(),
        "snapshot_time": snapshot.timestamp.isoformat(),
        "open": snapshot.last,
        "high": snapshot.last,
        "low": snapshot.last,
        "close": snapshot.last,
        "day_open": snapshot.open,
        "day_high": snapshot.high,
        "day_low": snapshot.low,
        "previous_close": snapshot.previous_close,
        "percent_change": snapshot.percent_change,
        "advances": snapshot.advances,
        "declines": snapshot.declines,
    }

    if candles and candles[-1]["bucket"] == candle["bucket"]:
        current = candles[-1]
        current["high"] = max(float(current["high"]), snapshot.last)
        current["low"] = min(float(current["low"]), snapshot.last)
        current["close"] = snapshot.last
        current["snapshot_time"] = snapshot.timestamp.isoformat()
        current["day_open"] = snapshot.open
        current["day_high"] = snapshot.high
        current["day_low"] = snapshot.low
        current["previous_close"] = snapshot.previous_close
        current["percent_change"] = snapshot.percent_change
        current["advances"] = snapshot.advances
        current["declines"] = snapshot.declines
    else:
        candles.append(candle)

    state["candles"] = candles[-300:]


def candles_to_frame(candles: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(candles)
    if frame.empty:
        return frame
    frame["time"] = pd.to_datetime(frame["bucket"])
    return frame


def enrich_candles(frame: pd.DataFrame) -> pd.DataFrame:
    candles = frame.copy()
    candles = candles.sort_values("time").reset_index(drop=True)
    session = candles["time"].dt.date

    if "day_open" not in candles:
        candles["day_open"] = candles.groupby(session)["open"].transform("first")
    if "day_high" not in candles:
        candles["day_high"] = candles.groupby(session)["high"].cummax()
    if "day_low" not in candles:
        candles["day_low"] = candles.groupby(session)["low"].cummin()
    if "previous_close" not in candles:
        day_last_close = candles.groupby(session)["close"].last().shift(1)
        candles["previous_close"] = session.map(day_last_close).astype("float64")
        candles["previous_close"] = candles["previous_close"].fillna(candles["close"].shift(1))
    if "percent_change" not in candles:
        candles["percent_change"] = (
            (candles["close"] - candles["previous_close"])
            / candles["previous_close"].replace(0, pd.NA)
            * 100
        ).fillna(0)
    if "advances" not in candles:
        candles["advances"] = pd.NA
    if "declines" not in candles:
        candles["declines"] = pd.NA

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
    candles["prev_macd_hist"] = candles["macd_hist"].shift(1)

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
    candles["prev_high_3"] = candles["high"].shift(1).rolling(3).max()
    candles["prev_low_3"] = candles["low"].shift(1).rolling(3).min()
    candles["body"] = candles["close"] - candles["open"]
    candles["body_strength"] = (
        candles["body"].abs() / candles["atr"].replace(0, pd.NA)
    ).fillna(0)
    candles["ema_gap_ratio"] = (
        (candles["ema_9"] - candles["ema_21"]).abs() / candles["close"].replace(0, pd.NA)
    ).fillna(0)
    candles["close_location"] = (
        (candles["close"] - candles["day_low"])
        / (candles["day_high"] - candles["day_low"]).replace(0, pd.NA)
    ).fillna(0.5)
    candles["breadth_available"] = candles["advances"].notna() & candles["declines"].notna()
    candles["breadth_ratio"] = (
        (candles["advances"] - candles["declines"])
        / (candles["advances"] + candles["declines"]).replace(0, pd.NA)
    ).fillna(0)
    return candles


def build_entry_signal_from_row(row: pd.Series) -> dict | None:
    required = [
        "ema_9",
        "ema_21",
        "rsi",
        "macd_hist",
        "prev_macd_hist",
        "atr",
        "prev_high_3",
        "prev_low_3",
    ]
    if any(pd.isna(row[col]) for col in required):
        return None

    atr = max(float(row["atr"]), 10.0)
    price = float(row["close"])
    percent_change = float(row["percent_change"])
    close_location = float(row["close_location"])
    breadth_ratio = float(row["breadth_ratio"])
    breakout_up = price > float(row["prev_high_3"])
    breakout_down = price < float(row["prev_low_3"])
    breakout_buffer_up = (price - float(row["prev_high_3"])) / atr
    breakout_buffer_down = (float(row["prev_low_3"]) - price) / atr
    macd_rising = float(row["macd_hist"]) > float(row["prev_macd_hist"])
    macd_falling = float(row["macd_hist"]) < float(row["prev_macd_hist"])
    body = float(row["body"])
    body_strength = float(row["body_strength"])
    ema_gap_ratio = float(row["ema_gap_ratio"])
    above_session_bias = price > float(row["day_open"]) and price > float(row["previous_close"])
    below_session_bias = price < float(row["day_open"]) and price < float(row["previous_close"])
    breadth_available = bool(row.get("breadth_available", False))

    bullish_score = 0.0
    bearish_score = 0.0

    if row["ema_9"] > row["ema_21"]:
        bullish_score += 20
    if ema_gap_ratio >= 0.0009:
        bullish_score += 10
    elif ema_gap_ratio >= 0.0005:
        bullish_score += 5
    if row["macd_hist"] > 0:
        bullish_score += 14
    if macd_rising:
        bullish_score += 8
    if 58 <= row["rsi"] <= 68:
        bullish_score += 14
    elif 55 <= row["rsi"] < 58 or 68 < row["rsi"] <= 71:
        bullish_score += 7
    if body > 0 and body_strength >= 0.20:
        bullish_score += 10
    elif body > 0 and body_strength >= 0.12:
        bullish_score += 5
    if breakout_up and breakout_buffer_up >= 0.15:
        bullish_score += 12
    elif breakout_up:
        bullish_score += 6
    if close_location >= 0.72:
        bullish_score += 8
    elif close_location >= 0.62:
        bullish_score += 4
    if above_session_bias:
        bullish_score += 10
    if percent_change >= 0.30:
        bullish_score += 6
    elif percent_change >= 0.18:
        bullish_score += 3
    if breadth_available:
        if breadth_ratio > 0.10:
            bullish_score += 8
        elif breadth_ratio > 0.05:
            bullish_score += 4

    if row["ema_9"] < row["ema_21"]:
        bearish_score += 20
    if ema_gap_ratio >= 0.0009:
        bearish_score += 10
    elif ema_gap_ratio >= 0.0005:
        bearish_score += 5
    if row["macd_hist"] < 0:
        bearish_score += 14
    if macd_falling:
        bearish_score += 8
    if 32 <= row["rsi"] <= 42:
        bearish_score += 14
    elif 29 <= row["rsi"] < 32 or 42 < row["rsi"] <= 45:
        bearish_score += 7
    if body < 0 and body_strength >= 0.20:
        bearish_score += 10
    elif body < 0 and body_strength >= 0.12:
        bearish_score += 5
    if breakout_down and breakout_buffer_down >= 0.15:
        bearish_score += 12
    elif breakout_down:
        bearish_score += 6
    if close_location <= 0.28:
        bearish_score += 8
    elif close_location <= 0.38:
        bearish_score += 4
    if below_session_bias:
        bearish_score += 10
    if percent_change <= -0.30:
        bearish_score += 6
    elif percent_change <= -0.18:
        bearish_score += 3
    if breadth_available:
        if breadth_ratio < -0.10:
            bearish_score += 8
        elif breadth_ratio < -0.05:
            bearish_score += 4

    bullish_valid = (
        row["ema_9"] > row["ema_21"]
        and row["macd_hist"] > 0
        and macd_rising
        and 55 <= row["rsi"] <= 71
        and body > 0
        and body_strength >= 0.12
        and breakout_up
        and breakout_buffer_up >= 0.08
        and above_session_bias
        and close_location >= 0.62
    )
    bearish_valid = (
        row["ema_9"] < row["ema_21"]
        and row["macd_hist"] < 0
        and macd_falling
        and 29 <= row["rsi"] <= 45
        and body < 0
        and body_strength >= 0.12
        and breakout_down
        and breakout_buffer_down >= 0.08
        and below_session_bias
        and close_location <= 0.38
    )

    strike = round_to_strike(price)
    if bullish_valid and bullish_score >= 78 and bullish_score > bearish_score + 6:
        return {
            "status": "BUY",
            "option_type": "CE",
            "strike": strike,
            "score": round(bullish_score, 2),
            "stop_loss": round(price - max(1.15 * atr, 16), 2),
            "target": round(price + max(2.0 * atr, 30), 2),
            "reason": (
                f"Confirmed 5-minute candle close for NIFTY {strike} CE. "
                f"Score {bullish_score:.1f}, RSI {row['rsi']:.1f}, "
                f"MACD improving, breakout above recent highs, pct {percent_change:.2f}%."
            ),
        }

    if bearish_valid and bearish_score >= 78 and bearish_score > bullish_score + 6:
        return {
            "status": "BUY",
            "option_type": "PE",
            "strike": strike,
            "score": round(bearish_score, 2),
            "stop_loss": round(price + max(1.15 * atr, 16), 2),
            "target": round(price - max(2.0 * atr, 30), 2),
            "reason": (
                f"Confirmed 5-minute candle close for NIFTY {strike} PE. "
                f"Score {bearish_score:.1f}, RSI {row['rsi']:.1f}, "
                f"MACD weakening, breakdown below recent lows, pct {percent_change:.2f}%."
            ),
        }

    return None


def load_trade(raw: dict | None) -> Trade | None:
    if not raw:
        return None
    return Trade(
        option_type=raw["option_type"],
        strike=int(raw["strike"]),
        entry_price=float(raw["entry_price"]),
        entry_time=datetime.fromisoformat(raw["entry_time"]),
        stop_loss=float(raw["stop_loss"]),
        target=float(raw["target"]),
        score=float(raw["score"]),
        status=raw.get("status", "OPEN"),
    )


def dump_trade(trade: Trade | None) -> dict | None:
    if not trade:
        return None
    payload = asdict(trade)
    payload["entry_time"] = trade.entry_time.isoformat()
    return payload


def evaluate_trade(trade: Trade, snapshot: LiveIndexSnapshot) -> tuple[str, float]:
    price = snapshot.last
    direction = 1 if trade.option_type == "CE" else -1
    pnl_points = round((price - trade.entry_price) * direction, 2)

    if trade.option_type == "CE":
        if price <= trade.stop_loss or price >= trade.target:
            return "SELL", pnl_points
    else:
        if price >= trade.stop_loss or price <= trade.target:
            return "SELL", pnl_points

    return "HOLD", pnl_points


def classify_signal_watch(row: pd.Series) -> tuple[str, str]:
    required = ["ema_9", "ema_21", "rsi", "macd_hist"]
    if any(pd.isna(row.get(col)) for col in required):
        return "Warming up", "Indicator context is still warming up."

    ema_9 = float(row["ema_9"])
    ema_21 = float(row["ema_21"])
    rsi = float(row["rsi"])
    macd_hist = float(row["macd_hist"])
    body_strength = float(row.get("body_strength", 0))
    close_location = float(row.get("close_location", 0.5))
    breakout_up = bool(
        not pd.isna(row.get("prev_high_3")) and float(row["close"]) > float(row["prev_high_3"])
    )
    breakout_down = bool(
        not pd.isna(row.get("prev_low_3")) and float(row["close"]) < float(row["prev_low_3"])
    )

    bullish_watch = ema_9 > ema_21 and macd_hist > 0 and rsi >= 55
    bearish_watch = ema_9 < ema_21 and macd_hist < 0 and rsi <= 45

    if bullish_watch and not bearish_watch:
        readiness = []
        if breakout_up:
            readiness.append("breakout seen")
        if body_strength >= 0.12:
            readiness.append("body strong")
        if close_location >= 0.62:
            readiness.append("close strong")
        readiness_text = ", ".join(readiness) if readiness else "waiting for cleaner breakout/body confirmation"
        return (
            "Bullish watch",
            (
                f"Bullish watch for a CE setup. EMA9 {ema_9:.2f} is above EMA21 {ema_21:.2f}, "
                f"RSI {rsi:.1f}, MACD hist {macd_hist:.2f}; {readiness_text}."
            ),
        )

    if bearish_watch and not bullish_watch:
        readiness = []
        if breakout_down:
            readiness.append("breakdown seen")
        if body_strength >= 0.12:
            readiness.append("body strong")
        if close_location <= 0.38:
            readiness.append("close weak")
        readiness_text = ", ".join(readiness) if readiness else "waiting for cleaner breakdown/body confirmation"
        return (
            "Bearish watch",
            (
                f"Bearish watch for a PE setup. EMA9 {ema_9:.2f} is below EMA21 {ema_21:.2f}, "
                f"RSI {rsi:.1f}, MACD hist {macd_hist:.2f}; {readiness_text}."
            ),
        )

    return (
        "Neutral wait",
        (
            f"Wait. No clean directional edge yet. EMA9 {ema_9:.2f}, EMA21 {ema_21:.2f}, "
            f"RSI {rsi:.1f}, MACD hist {macd_hist:.2f}."
        ),
    )


def apply_signal_change_alert(state: dict, result: TrackerResult) -> tuple[TrackerResult, dict]:
    signature = result.alert_key()
    previous_signature = state.get("last_signal_signature")
    signal_changed = previous_signature is not None and previous_signature != signature
    state["last_signal_signature"] = signature

    if signal_changed and result.status not in {"BUY", "SELL"}:
        result = replace(result, should_alert=True, signal_changed=True)
    elif signal_changed:
        result = replace(result, signal_changed=True)

    return result, state


def should_send_hold_alert(state: dict, current: datetime, hold_interval_minutes: int) -> bool:
    last = parse_dt(state.get("last_hold_alert_at"))
    if last is None:
        return True
    return current - last >= timedelta(minutes=hold_interval_minutes)


def should_send_hourly_alert(state: dict, current: datetime, hourly_interval_minutes: int) -> bool:
    last = parse_dt(state.get("last_hourly_alert_at"))
    if last is None:
        return True
    return current - last >= timedelta(minutes=hourly_interval_minutes)


def run_live_cycle(
    state: dict,
    max_data_age_minutes: int,
    hold_interval_minutes: int,
    hourly_interval_minutes: int,
) -> tuple[TrackerResult, dict]:
    snapshot = fetch_nse_live_snapshot()
    data_age = now_ist() - snapshot.timestamp
    append_snapshot_candle(state, snapshot)

    if data_age > timedelta(minutes=max_data_age_minutes):
        result = TrackerResult(
            status="WAIT",
            title="NIFTY Signal",
            spot_price=snapshot.last,
            time=snapshot.timestamp,
            reason=(
                f"Stale NSE live data. Latest update is {data_age}. "
                f"Allowed maximum is {max_data_age_minutes} minutes."
            ),
            signal_state="Stale data",
            should_alert=should_send_hourly_alert(state, now_ist(), hourly_interval_minutes),
            hourly_summary=should_send_hourly_alert(state, now_ist(), hourly_interval_minutes),
        )
        return apply_signal_change_alert(state, result)

    active_trade = load_trade(state.get("active_trade"))
    current = now_ist()

    if active_trade:
        status, pnl_points = evaluate_trade(active_trade, snapshot)
        if status == "SELL":
            state["active_trade"] = None
            result = TrackerResult(
                status="SELL",
                title=f"NIFTY {active_trade.strike} {active_trade.option_type}",
                spot_price=snapshot.last,
                time=snapshot.timestamp,
                reason=(
                    f"Exit {active_trade.option_type} because stop/target was hit or close condition changed."
                ),
                option_type=active_trade.option_type,
                strike=active_trade.strike,
                score=active_trade.score,
                stop_loss=active_trade.stop_loss,
                target=active_trade.target,
                pnl_points=pnl_points,
                should_alert=True,
            )
            return result, state

        send_hold = should_send_hold_alert(state, current, hold_interval_minutes)
        hourly_summary = should_send_hourly_alert(state, current, hourly_interval_minutes)
        result = TrackerResult(
            status="HOLD",
            title=f"NIFTY {active_trade.strike} {active_trade.option_type}",
            spot_price=snapshot.last,
            time=snapshot.timestamp,
            reason="Trade is active. Hold until stop, target, or reverse confirmation.",
            option_type=active_trade.option_type,
            strike=active_trade.strike,
            score=active_trade.score,
            stop_loss=active_trade.stop_loss,
            target=active_trade.target,
            pnl_points=pnl_points,
            signal_state=f"Active {active_trade.option_type}",
            should_alert=send_hold or hourly_summary,
            hourly_summary=hourly_summary,
        )
        return apply_signal_change_alert(state, result)

    frame = candles_to_frame(state["candles"])
    enriched = enrich_candles(frame)
    hourly_summary = should_send_hourly_alert(state, current, hourly_interval_minutes)
    if enriched.empty:
        result = TrackerResult(
            status="WAIT",
            title="NIFTY Signal",
            spot_price=snapshot.last,
            time=snapshot.timestamp,
            reason="No candle history yet. Waiting for live data to build 5-minute candle context.",
            signal_state="Warming up",
            should_alert=hourly_summary,
            hourly_summary=hourly_summary,
        )
        return apply_signal_change_alert(state, result)

    if len(enriched) < 25:
        result = TrackerResult(
            status="WAIT",
            title="NIFTY Signal",
            spot_price=snapshot.last,
            time=snapshot.timestamp,
            reason=(
                f"Warming up candle history. Need about 25 stored 5-minute candles, "
                f"currently have {len(enriched)}."
            ),
            signal_state="Warming up",
            should_alert=hourly_summary,
            hourly_summary=hourly_summary,
        )
        return apply_signal_change_alert(state, result)

    latest = enriched.iloc[-1]
    bucket = latest["time"].isoformat()
    entry = build_entry_signal_from_row(latest)
    watch_state, watch_reason = classify_signal_watch(latest)

    if entry and state.get("last_signal_bucket") != bucket:
        trade = Trade(
            option_type=entry["option_type"],
            strike=entry["strike"],
            entry_price=float(snapshot.last),
            entry_time=snapshot.timestamp,
            stop_loss=entry["stop_loss"],
            target=entry["target"],
            score=entry["score"],
        )
        state["active_trade"] = dump_trade(trade)
        state["last_signal_bucket"] = bucket
        result = TrackerResult(
            status="BUY",
            title=f"NIFTY {trade.strike} {trade.option_type}",
            spot_price=snapshot.last,
            time=snapshot.timestamp,
            reason=entry["reason"],
            option_type=trade.option_type,
            strike=trade.strike,
            score=trade.score,
            stop_loss=trade.stop_loss,
            target=trade.target,
            signal_state=f"Active {trade.option_type}",
            should_alert=True,
        )
        return apply_signal_change_alert(state, result)

    result = TrackerResult(
        status="WAIT",
        title="NIFTY Signal",
        spot_price=snapshot.last,
        time=snapshot.timestamp,
        reason=watch_reason,
        signal_state=watch_state,
        should_alert=hourly_summary,
        hourly_summary=hourly_summary,
    )
    return apply_signal_change_alert(state, result)


def format_result(result: TrackerResult) -> str:
    lines = [
        f"NIFTY Status: {result.status}",
        f"Time: {result.time:%Y-%m-%d %H:%M %Z}",
        f"Spot price: {result.spot_price:.2f}",
    ]
    if result.option_type and result.strike:
        lines.append(f"Option idea: NIFTY {result.strike} {result.option_type}")
    if result.signal_state:
        lines.append(f"Signal watch: {result.signal_state}")
    if result.score:
        lines.append(f"Score: {result.score:.2f}")
    lines.append(f"Reason: {result.reason}")
    if result.stop_loss is not None:
        lines.append(f"Underlying stop-loss: {result.stop_loss:.2f}")
    if result.target is not None:
        lines.append(f"Underlying target: {result.target:.2f}")
    if result.pnl_points is not None:
        lines.append(f"Running P&L (underlying points): {result.pnl_points:.2f}")
    if result.signal_changed:
        lines.append("Signal change: yes")
    if result.hourly_summary:
        lines.append("Hourly summary: yes")
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


def format_table(headers: list[str], rows: list[list[object]]) -> str:
    if not rows:
        return "No rows"

    widths = [len(header) for header in headers]
    normalized_rows: list[list[str]] = []
    for row in rows:
        normalized = [str(cell) for cell in row]
        normalized_rows.append(normalized)
        widths = [max(width, len(cell)) for width, cell in zip(widths, normalized)]

    header_line = " | ".join(header.ljust(width) for header, width in zip(headers, widths))
    separator = "-+-".join("-" * width for width in widths)
    row_lines = [
        " | ".join(cell.ljust(width) for cell, width in zip(row, widths))
        for row in normalized_rows
    ]
    return "\n".join([header_line, separator, *row_lines])


def max_consecutive(values: list[bool], target: bool) -> int:
    best = 0
    current = 0
    for value in values:
        if value is target:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def build_backtest_report(trades: list[dict], range_: str, open_trade: dict | None = None) -> str:
    if not trades:
        lines = [
            "Backtest Summary",
            f"Range: {range_}",
            "Trades: 0",
            "No completed trades for the current tighter filters.",
        ]
        if open_trade:
            lines.append(
                f"Open trade at end: {open_trade['option_type']} {open_trade['strike']} "
                f"from {open_trade['entry_time']:%Y-%m-%d %H:%M}"
            )
        return "\n".join(lines)

    trade_frame = pd.DataFrame(trades)
    trade_frame["result"] = trade_frame["pnl_points"].apply(lambda pnl: "WIN" if pnl > 0 else "LOSS")
    trade_frame["cum_pnl"] = trade_frame["pnl_points"].cumsum()
    trade_frame["equity_peak"] = trade_frame["cum_pnl"].cummax()
    trade_frame["drawdown"] = trade_frame["equity_peak"] - trade_frame["cum_pnl"]

    total_trades = len(trade_frame)
    wins = int((trade_frame["result"] == "WIN").sum())
    losses = int((trade_frame["result"] == "LOSS").sum())
    win_rate = (wins / total_trades) * 100 if total_trades else 0
    total_pnl = float(trade_frame["pnl_points"].sum())
    avg_pnl = float(trade_frame["pnl_points"].mean())
    gross_profit = float(trade_frame.loc[trade_frame["pnl_points"] > 0, "pnl_points"].sum())
    gross_loss = float(trade_frame.loc[trade_frame["pnl_points"] < 0, "pnl_points"].sum())
    avg_win = float(trade_frame.loc[trade_frame["pnl_points"] > 0, "pnl_points"].mean()) if wins else 0
    avg_loss = float(trade_frame.loc[trade_frame["pnl_points"] < 0, "pnl_points"].mean()) if losses else 0
    profit_factor = gross_profit / abs(gross_loss) if gross_loss else float("inf")
    max_drawdown = float(trade_frame["drawdown"].max())
    results = trade_frame["result"].eq("WIN").tolist()
    max_win_streak = max_consecutive(results, True)
    max_loss_streak = max_consecutive(results, False)

    summary_lines = [
        "Backtest Summary",
        f"Range: {range_}",
        f"Trades: {total_trades}",
        f"Wins: {wins}",
        f"Losses: {losses}",
        f"Win rate: {win_rate:.2f}%",
        f"Total P&L (underlying points): {total_pnl:.2f}",
        f"Average P&L per trade: {avg_pnl:.2f}",
        f"Average win: {avg_win:.2f}",
        f"Average loss: {avg_loss:.2f}",
        f"Profit factor: {profit_factor:.2f}" if math.isfinite(profit_factor) else "Profit factor: inf",
        f"Max drawdown (points): {max_drawdown:.2f}",
        f"Max consecutive wins: {max_win_streak}",
        f"Max consecutive losses: {max_loss_streak}",
    ]

    option_summary = (
        trade_frame.groupby("option_type")
        .agg(
            trades=("pnl_points", "size"),
            wins=("result", lambda values: int((values == "WIN").sum())),
            losses=("result", lambda values: int((values == "LOSS").sum())),
            total_pnl=("pnl_points", "sum"),
            avg_pnl=("pnl_points", "mean"),
            best=("pnl_points", "max"),
            worst=("pnl_points", "min"),
        )
        .reset_index()
    )
    option_rows = [
        [
            row["option_type"],
            int(row["trades"]),
            int(row["wins"]),
            int(row["losses"]),
            f"{(row['wins'] / row['trades']) * 100:.1f}%",
            f"{row['total_pnl']:.2f}",
            f"{row['avg_pnl']:.2f}",
            f"{row['best']:.2f}",
            f"{row['worst']:.2f}",
        ]
        for _, row in option_summary.iterrows()
    ]

    win_loss_rows = [
        [
            outcome,
            len(group),
            f"{group['pnl_points'].sum():.2f}",
            f"{group['pnl_points'].mean():.2f}",
            f"{group['bars_held'].mean():.1f}",
        ]
        for outcome, group in trade_frame.groupby("result")
    ]

    recent_rows = [
        [
            row["entry_time"].strftime("%m-%d %H:%M"),
            row["exit_time"].strftime("%m-%d %H:%M"),
            row["option_type"],
            row["strike"],
            row["exit_reason"],
            row["bars_held"],
            f"{row['pnl_points']:.2f}",
        ]
        for _, row in trade_frame.tail(8).iterrows()
    ]

    lines = summary_lines + [
        "",
        "Option Side Table",
        format_table(
            ["Side", "Trades", "Wins", "Losses", "Win%", "TotalPnL", "AvgPnL", "Best", "Worst"],
            option_rows,
        ),
        "",
        "Win/Loss Table",
        format_table(
            ["Result", "Trades", "TotalPnL", "AvgPnL", "AvgBars"],
            win_loss_rows,
        ),
        "",
        "Recent Trades",
        format_table(
            ["Entry", "Exit", "Opt", "Strike", "ExitReason", "Bars", "PnL"],
            recent_rows,
        ),
    ]

    if open_trade:
        lines.extend(
            [
                "",
                "Open trade at end:",
                (
                    f"{open_trade['option_type']} {open_trade['strike']} from "
                    f"{open_trade['entry_time']:%Y-%m-%d %H:%M}"
                ),
            ]
        )
    return "\n".join(lines)


def run_backtest(range_: str = "30d") -> str:
    candles = enrich_candles(fetch_yahoo_candles(range_=range_))
    active: dict | None = None
    trades: list[dict] = []

    for idx, row in candles.iterrows():
        price = float(row["close"])
        if active:
            direction = 1 if active["option_type"] == "CE" else -1
            pnl = (price - active["entry_price"]) * direction
            exit_hit = (
                price <= active["stop_loss"] or price >= active["target"]
                if active["option_type"] == "CE"
                else price >= active["stop_loss"] or price <= active["target"]
            )
            if exit_hit:
                trades.append(
                    {
                        "entry_time": active["entry_time"],
                        "exit_time": row["time"],
                        "option_type": active["option_type"],
                        "strike": active["strike"],
                        "score": active["score"],
                        "entry_price": active["entry_price"],
                        "exit_price": price,
                        "bars_held": idx - active["entry_index"],
                        "pnl_points": round(pnl, 2),
                        "exit_reason": "target_or_stop",
                    }
                )
                active = None
                continue

        if not active:
            entry = build_entry_signal_from_row(row)
            if entry:
                active = {
                    "entry_time": row["time"],
                    "entry_index": idx,
                    "option_type": entry["option_type"],
                    "strike": entry["strike"],
                    "score": entry["score"],
                    "entry_price": price,
                    "stop_loss": entry["stop_loss"],
                    "target": entry["target"],
                }

    return build_backtest_report(trades, range_, open_trade=active)


def run_live_once(
    *,
    ignore_market_hours: bool,
    max_data_age_minutes: int,
    hold_update_minutes: int,
    hourly_summary_minutes: int,
) -> None:
    state = load_state()
    if not ignore_market_hours and not is_market_open():
        result = TrackerResult(
            status="WAIT",
            title="NIFTY Signal",
            spot_price=0,
            time=now_ist(),
            reason="Market is closed. Waiting for next NSE session.",
            signal_state="Market closed",
            should_alert=False,
        )
        print(format_result(result))
        print()
        return

    result, updated_state = run_live_cycle(
        state=state,
        max_data_age_minutes=max_data_age_minutes,
        hold_interval_minutes=hold_update_minutes,
        hourly_interval_minutes=hourly_summary_minutes,
    )
    print(format_result(result))
    print()

    if result.should_alert:
        send_telegram(format_result(result))
        if result.status == "HOLD":
            updated_state["last_hold_alert_at"] = to_iso(now_ist())
        if result.hourly_summary:
            updated_state["last_hourly_alert_at"] = to_iso(now_ist())

    save_state(updated_state)


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY live option tracker")
    parser.add_argument("--once", action="store_true", help="Run a single live cycle and exit.")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of exiting after one pass.")
    parser.add_argument("--backtest", action="store_true", help="Run historical backtest instead of live tracking.")
    parser.add_argument("--backtest-range", default="30d", help="Yahoo range for backtest, for example 30d or 60d.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Run even outside NSE market hours.")
    parser.add_argument("--max-data-age-minutes", type=int, default=15)
    parser.add_argument("--hold-update-minutes", type=int, default=DEFAULT_HOLD_INTERVAL_MINUTES)
    parser.add_argument("--hourly-summary-minutes", type=int, default=DEFAULT_HOURLY_INTERVAL_MINUTES)
    parser.add_argument("--poll-seconds", type=int, default=120, help="Sleep interval between cycles in loop mode.")
    args = parser.parse_args()

    load_dotenv()

    if args.backtest:
        print(run_backtest(range_=args.backtest_range))
        return

    if args.loop:
        while True:
            run_live_once(
                ignore_market_hours=args.ignore_market_hours,
                max_data_age_minutes=args.max_data_age_minutes,
                hold_update_minutes=args.hold_update_minutes,
                hourly_summary_minutes=args.hourly_summary_minutes,
            )
            time.sleep(max(args.poll_seconds, 30))
        return

    run_live_once(
        ignore_market_hours=args.ignore_market_hours,
        max_data_age_minutes=args.max_data_age_minutes,
        hold_update_minutes=args.hold_update_minutes,
        hourly_summary_minutes=args.hourly_summary_minutes,
    )


if __name__ == "__main__":
    main()
