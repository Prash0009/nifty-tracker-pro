from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo
from collections import deque
import json

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
}

SignalSide = Literal["BUY", "SELL", "WAIT", "HOLD"]

type Signal {
    side SignalSide
    price float
    time datetime
    reason str
    instrument str = "NIFTY"
    option_type str | None = None
    strike int | None = None
    score float = 0
    stop_loss float | None = None
    target float | None = None
    entry_time datetime | None = None
    candle_pattern str = ""
    details tuple["Signal", ...] = ()

    def alert_key(self) -> str:
        return (
            f"{self.time.isoformat()}:{self.instrument}:{self.side}:"
            f"{self.option_type or 'NA'}:{self.strike or 'NA'}:{round(self.price, 2)}"
        )
}

type LiveIndexSnapshot {
    index str
    last float
    open float
    high float
    low float
    previous_close float
    percent_change float
    advances int
    declines int
    timestamp datetime
}

type TradingState {
    """Tracks active trading state for HOLD updates"""
    active_signal Signal | None = None
    entry_price float | None = None
    entry_time datetime | None = None
    last_update_time datetime | None = None
    update_interval_minutes int = 2
    
    def needs_update(self) -> bool:
        if not self.last_update_time:
            return True
        return (datetime.now(IST) - self.last_update_time).total_seconds() >= self.update_interval_minutes * 60
}

def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False

    market_open = clock_time(9, 15)
    market_close = clock_time(15, 30)
    return market_open <= now.time() <= market_close


def fetch_nse_live_snapshot(index_name: str = NSE_INDEX_NAME) -> LiveIndexSnapshot:
    """Fetch single NSE live snapshot for NIFTY 50 only"""
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
    
    row = next((item for item in rows if item.get("index") == index_name), None)
    if not row:
        raise RuntimeError(f"Missing NSE live data for: {index_name}")

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
    """Add technical indicators with enhanced calculations"""
    candles = frame.copy()

    # EMAs
    candles["ema_9"] = candles["close"].ewm(span=9, adjust=False).mean()
    candles["ema_21"] = candles["close"].ewm(span=21, adjust=False).mean()
    candles["ema_50"] = candles["close"].ewm(span=50, adjust=False).mean()

    # RSI (14)
    delta = candles["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    candles["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema_12 = candles["close"].ewm(span=12, adjust=False).mean()
    ema_26 = candles["close"].ewm(span=26, adjust=False).mean()
    candles["macd"] = ema_12 - ema_26
    candles["macd_signal"] = candles["macd"].ewm(span=9, adjust=False).mean()
    candles["macd_hist"] = candles["macd"] - candles["macd_signal"]

    # ATR
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

    # Bollinger Bands
    candles["bb_middle"] = candles["close"].rolling(20).mean()
    candles["bb_std"] = candles["close"].rolling(20).std()
    candles["bb_upper"] = candles["bb_middle"] + (candles["bb_std"] * 2)
    candles["bb_lower"] = candles["bb_middle"] - (candles["bb_std"] * 2)

    # Volume-based momentum
    candles["volume_sma"] = candles["close"].rolling(5).std() / candles["close"].rolling(5).mean()

    return candles


def detect_candle_pattern(candles: pd.DataFrame) -> tuple[str, float]:
    """Detect candle patterns for entry timing"""
    if len(candles) < 3:
        return "NO_PATTERN", 0.0
    
    latest = candles.iloc[-1]
    prev = candles.iloc[-2]
    prev2 = candles.iloc[-3]
    
    # Pattern scoring
    pattern_score = 0.0
    pattern_name = "NO_PATTERN"
    
    # Engulfing pattern
    if (latest["open"] < prev["open"] and 
        latest["close"] > prev["close"] and
        latest["close"] > latest["open"]):
        pattern_name = "BULLISH_ENGULFING"
        pattern_score = 2.0
    elif (latest["open"] > prev["open"] and 
          latest["close"] < prev["close"] and
          latest["close"] < latest["open"]):
        pattern_name = "BEARISH_ENGULFING"
        pattern_score = -2.0
    
    # Hammer pattern (bullish reversal)
    elif (latest["low"] < prev["close"] and 
          latest["close"] > latest["open"] and
          (latest["high"] - latest["close"]) < (latest["close"] - latest["open"])):
        pattern_name = "HAMMER"
        pattern_score = 1.5
    
    # Shooting star (bearish reversal)
    elif (latest["high"] > prev["close"] and 
          latest["close"] < latest["open"] and
          (latest["high"] - latest["close"]) > (latest["close"] - latest["open"])):
        pattern_name = "SHOOTING_STAR"
        pattern_score = -1.5
    
    # Three white soldiers (bullish)
    elif (prev2["close"] > prev2["open"] and
          prev["close"] > prev["open"] and
          latest["close"] > latest["open"] and
          latest["close"] > prev["close"] and
          prev["close"] > prev2["close"]):
        pattern_name = "THREE_WHITE_SOLDIERS"
        pattern_score = 2.5
    
    # Three black crows (bearish)
    elif (prev2["close"] < prev2["open"] and
          prev["close"] < prev["open"] and
          latest["close"] < latest["open"] and
          latest["close"] < prev["close"] and
          prev["close"] < prev2["close"]):
        pattern_name = "THREE_BLACK_CROWS"
        pattern_score = -2.5
    
    return pattern_name, pattern_score


def calculate_composite_score(candles: pd.DataFrame, snapshot: LiveIndexSnapshot | None = None) -> tuple[float, dict]:
    """Calculate weighted composite score using all filters"""
    latest = candles.iloc[-1]
    scores = {}
    
    # EMA Trend Score (25% weight)
    if latest["ema_9"] > latest["ema_21"]:
        ema_score = 1.0
        scores["ema_trend"] = ema_score
    elif latest["ema_9"] < latest["ema_21"]:
        ema_score = -1.0
        scores["ema_trend"] = ema_score
    else:
        ema_score = 0.0
        scores["ema_trend"] = ema_score
    
    # MACD Momentum Score (25% weight)
    if latest["macd_hist"] > 0 and latest["macd"] > latest["macd_signal"]:
        macd_score = 1.0 + (min(latest["macd_hist"] / abs(latest["macd"]), 0.5) if latest["macd"] != 0 else 0)
        scores["macd_momentum"] = macd_score
    elif latest["macd_hist"] < 0 and latest["macd"] < latest["macd_signal"]:
        macd_score = -1.0 - (min(abs(latest["macd_hist"]) / abs(latest["macd"]), 0.5) if latest["macd"] != 0 else 0)
        scores["macd_momentum"] = macd_score
    else:
        macd_score = 0.0
        scores["macd_momentum"] = macd_score
    
    # RSI Strength Score (20% weight)
    if 50 < latest["rsi"] <= 72:
        rsi_score = (latest["rsi"] - 50) / 22.0
        scores["rsi_strength"] = rsi_score
    elif 28 <= latest["rsi"] < 50:
        rsi_score = -(50 - latest["rsi"]) / 22.0
        scores["rsi_strength"] = rsi_score
    else:
        rsi_score = 0.0
        scores["rsi_strength"] = rsi_score
    
    # Bollinger Bands Score (15% weight)
    if latest["close"] > latest["bb_upper"]:
        bb_score = -0.5  # Overbought
        scores["bb_position"] = bb_score
    elif latest["close"] < latest["bb_lower"]:
        bb_score = 0.5   # Oversold
        scores["bb_position"] = bb_score
    else:
        bb_score = ((latest["close"] - latest["bb_lower") / (latest["bb_upper"] - latest["bb_lower"]) - 0.5) * 0.5
        scores["bb_position"] = bb_score
    
    # Candle Pattern Score (15% weight)
    pattern_name, pattern_score = detect_candle_pattern(candles)
    scores["candle_pattern"] = pattern_score
    scores["pattern_name"] = pattern_name
    
    # NSE Live Breadth Score (if available - 10% weight)
    breadth_score = 0.0
    if snapshot:
        breadth_total = max(snapshot.advances + snapshot.declines, 1)
        breadth_strength = (snapshot.advances - snapshot.declines) / breadth_total
        breadth_score = breadth_strength
        scores["breadth"] = breadth_score
    
    # Composite weighted score
    weights = {
        "ema_trend": 0.25,
        "macd_momentum": 0.25,
        "rsi_strength": 0.20,
        "bb_position": 0.15,
        "candle_pattern": 0.15,
    }
    if snapshot:
        weights["breadth"] = 0.10
        for k, v in weights.items():
            if k != "breadth":
                weights[k] = v * 0.9  # Reduce other weights
    
    composite_score = sum(scores.get(k, 0) * v for k, v in weights.items())
    return composite_score, scores


def nearest_strike(price: float, strike_step: int) -> int:
    return int(round(price / strike_step) * strike_step)


def build_signal(candles: pd.DataFrame, snapshot: LiveIndexSnapshot | None = None, max_candle_age_minutes: int = 15) -> Signal:
    """Build signal with enhanced entry timing and composite scoring"""
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

    composite_score, scores = calculate_composite_score(candles, snapshot)
    pattern_name = scores.get("pattern_name", "NO_PATTERN")
    
    # Strong bullish setup
    bullish = (
        latest["ema_9"] > latest["ema_21"] and 
        latest["macd_hist"] > 0 and 
        50 < latest["rsi"] <= 72 and
        composite_score > 0.5 and
        pattern_name in ["BULLISH_ENGULFING", "HAMMER", "THREE_WHITE_SOLDIERS"]
    )
    
    # Strong bearish setup
    bearish = (
        latest["ema_9"] < latest["ema_21"] and 
        latest["macd_hist"] < 0 and 
        28 <= latest["rsi"] < 50 and
        composite_score < -0.5 and
        pattern_name in ["BEARISH_ENGULFING", "SHOOTING_STAR", "THREE_BLACK_CROWS"]
    )

    if bullish:
        strike = nearest_strike(price, 50)
        return Signal(
            side="BUY",
            price=price,
            time=candle_time,
            entry_time=candle_time,
            instrument="NIFTY",
            option_type="CE",
            strike=strike,
            score=composite_score,
            candle_pattern=pattern_name,
            reason=(
                f"Strong bullish setup: {pattern_name} detected. "
                f"EMA uptrend (9>{latest['ema_9']:.0f} > 21>{latest['ema_21']:.0f}), "
                f"MACD positive, RSI {latest['rsi']:.1f}. Score: {composite_score:.2f}"
            ),
            stop_loss=price - (1.5 * atr),
            target=price + (2.5 * atr),
        )

    if bearish:
        strike = nearest_strike(price, 50)
        return Signal(
            side="SELL",
            price=price,
            time=candle_time,
            entry_time=candle_time,
            instrument="NIFTY",
            option_type="PE",
            strike=strike,
            score=abs(composite_score),
            candle_pattern=pattern_name,
            reason=(
                f"Strong bearish setup: {pattern_name} detected. "
                f"EMA downtrend (9>{latest['ema_9']:.0f} < 21>{latest['ema_21']:.0f}), "
                f"MACD negative, RSI {latest['rsi']:.1f}. Score: {abs(composite_score):.2f}"
            ),
            stop_loss=price + (1.5 * atr),
            target=price - (2.5 * atr),
        )

    return Signal(
        side="WAIT",
        price=price,
        time=candle_time,
        candle_pattern=pattern_name,
        score=abs(composite_score),
        reason=(
            f"No clean setup. {pattern_name}. "
            f"EMA: 9>{latest['ema_9']:.0f} vs 21>{latest['ema_21']:.0f}, "
            f"MACD hist {latest['macd_hist']:.2f}, RSI {latest['rsi']:.1f}, "
            f"Composite score: {composite_score:.2f}"
        ),
    )


def format_signal(signal: Signal, hold_duration: timedelta | None = None) -> str:
    """Format signal with entry timing details"""
    title = f"{signal.instrument} Signal"
    if signal.option_type and signal.strike:
        title = f"{signal.instrument} {signal.strike} {signal.option_type} Signal"

    lines = [
        f"{'='*50}",
        f"{title}: {signal.side}",
        f"Time: {signal.time:%Y-%m-%d %H:%M %Z}",
        f"Spot price: ₹{signal.price:.2f}",
    ]

    if signal.option_type and signal.strike:
        lines.append(f"Action: {signal.side} {signal.instrument} {signal.strike} {signal.option_type}")
    
    if signal.candle_pattern and signal.candle_pattern != "NO_PATTERN":
        lines.append(f"Pattern: {signal.candle_pattern}")
    
    lines.append(f"Score: {signal.score:.2f}")
    lines.append(f"Reason: {signal.reason}")

    if signal.stop_loss is not None and signal.target is not None:
        lines.append(f"SL: ₹{signal.stop_loss:.2f} | Target: ₹{signal.target:.2f}")
    
    if hold_duration:
        lines.append(f"Trade duration: {hold_duration}")
    
    lines.append(f"{'='*50}")

    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    """Send message to Telegram"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def format_hold_update(trading_state: TradingState, current_price: float) -> str:
    """Format HOLD status update"""
    if not trading_state.active_signal:
        return "No active trade"
    
    signal = trading_state.active_signal
    duration = datetime.now(IST) - trading_state.entry_time if trading_state.entry_time else None
    pnl = ((current_price - trading_state.entry_price) / trading_state.entry_price * 100) if trading_state.entry_price else 0
    
    lines = [
        f"📊 HOLD UPDATE - {datetime.now(IST):%H:%M}",
        f"Position: {signal.side} {signal.instrument} {signal.strike} {signal.option_type}",
        f"Entry: ₹{trading_state.entry_price:.2f} @ {trading_state.entry_time:%H:%M}",
        f"Current: ₹{current_price:.2f} | P&L: {pnl:+.2f}%%",
        f"Duration: {duration}",
        f"Target: ₹{signal.target:.2f} | SL: ₹{signal.stop_loss:.2f}",
    ]
    
    return "\n".join(lines)


def seconds_until_next_5m(now: datetime | None = None) -> int:
    now = now or datetime.now(IST)
    next_run = now.replace(second=10, microsecond=0)
    minutes_to_add = 5 - (now.minute % 5)
    next_run += timedelta(minutes=minutes_to_add)

    delta = (next_run - now).total_seconds()
    return max(30, int(delta))


def run_cycle(trading_state: TradingState, max_candle_age_minutes: int = 15) -> tuple[Signal, bool]:
    """Run single analysis cycle and return signal + whether to send alert"""
    try:
        candles = add_indicators(fetch_candles())
        snapshot = fetch_nse_live_snapshot()
        
        signal = build_signal(candles, snapshot, max_candle_age_minutes=max_candle_age_minutes)
        
        # Check if 2-minute HOLD update is needed
        send_hold_update = False
        if trading_state.active_signal and trading_state.active_signal.side in ["BUY", "SELL"]:
            if trading_state.needs_update():
                send_hold_update = True
                trading_state.last_update_time = datetime.now(IST)
        
        # Update trading state on new signal
        if signal.side in ["BUY", "SELL"] and signal.side != trading_state.active_signal?.side:
            trading_state.active_signal = signal
            trading_state.entry_price = signal.price
            trading_state.entry_time = signal.time
        
        return signal, send_hold_update
        
    except Exception as e:
        print(f"Cycle error: {e}")
        return Signal(
            side="WAIT",
            price=0,
            time=datetime.now(IST),
            reason=f"Error: {str(e)}"
        ), False


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY 50 5-minute signal tracker with auto-trading prep")
    parser.add_argument("--once", action="store_true", help="Run a single signal check and exit.")
    parser.add_argument("--send-wait-alerts", action="store_true", help="Also send Telegram alerts for WAIT signals.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Run even outside NSE market hours.")
    parser.add_argument(
        "--max-candle-age-minutes",
        type=int,
        default=15,
        help="Do not produce BUY/SELL alerts when the latest candle is older than this many minutes.",
    )
    args = parser.parse_args()

    load_dotenv()
    last_alert_key: str | None = None
    trading_state = TradingState()
    last_hourly_update = datetime.now(IST)

    while True:
        try:
            if args.ignore_market_hours or is_market_open():
                signal, send_hold_update = run_cycle(trading_state, max_candle_age_minutes=args.max_candle_age_minutes)
                
                # Main signal alert
                if signal.alert_key() != last_alert_key:
                    if signal.side != "WAIT" or args.send_wait_alerts:
                        message = format_signal(signal)
                        print(message)
                        send_telegram(message)
                    last_alert_key = signal.alert_key()
                
                # 2-minute HOLD updates
                if send_hold_update and trading_state.active_signal:
                    hold_message = format_hold_update(trading_state, signal.price)
                    print(hold_message)
                    send_telegram(hold_message)
                
                # Hourly status update
                now = datetime.now(IST)
                if (now - last_hourly_update).total_seconds() >= 3600:
                    status = f"⏰ HOURLY STATUS - {now:%H:%M}\n"
                    status += f"Status: {signal.side}\n"
                    status += f"Price: ₹{signal.price:.2f}\n"
                    if signal.side != "WAIT":
                        status += f"Action: {signal.side} {signal.instrument} {signal.strike} {signal.option_type if signal.option_type else 'N/A'}\n"
                    status += f"Pattern: {signal.candle_pattern}\n"
                    status += f"Reason: {signal.reason[:150]}..."
                    
                    print(status)
                    send_telegram(status)
                    last_hourly_update = now
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
