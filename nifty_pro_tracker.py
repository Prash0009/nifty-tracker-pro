from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
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
    should_alert: bool = False
    hourly_summary: bool = False

    def alert_key(self) -> str:
        return (
            f"{self.time.isoformat()}:{self.status}:{self.option_type or 'NA'}:"
            f"{self.strike or 'NA'}:{round(self.spot_price, 2)}:{int(self.hourly_summary)}"
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
    candles["prev_high_3"] = candles["high"].shift(1).rolling(3).max()
    candles["prev_low_3"] = candles["low"].shift(1).rolling(3).min()
    candles["close_location"] = (
        (candles["close"] - candles["day_low"])
        / (candles["day_high"] - candles["day_low"]).replace(0, pd.NA)
    ).fillna(0.5)
    candles["breadth_ratio"] = (
        (candles["advances"] - candles["declines"])
        / (candles["advances"] + candles["declines"]).replace(0, pd.NA)
    ).fillna(0)
    return candles


def build_entry_signal_from_row(row: pd.Series) -> dict | None:
    if any(pd.isna(row[col]) for col in ["ema_9", "ema_21", "rsi", "macd_hist", "atr"]):
        return None

    atr = max(float(row["atr"]), 10.0)
    price = float(row["close"])
    percent_change = float(row["percent_change"])
    close_location = float(row["close_location"])
    breadth_ratio = float(row["breadth_ratio"])
    breakout_up = pd.notna(row["prev_high_3"]) and price > float(row["prev_high_3"])
    breakout_down = pd.notna(row["prev_low_3"]) and price < float(row["prev_low_3"])

    bullish_score = 0.0
    bearish_score = 0.0

    if row["ema_9"] > row["ema_21"]:
        bullish_score += 20
    if row["macd_hist"] > 0:
        bullish_score += 18
    if 54 <= row["rsi"] <= 72:
        bullish_score += 16
    if percent_change >= 0.18:
        bullish_score += min(percent_change * 50, 15)
    if breadth_ratio > 0.08:
        bullish_score += min(breadth_ratio * 100, 15)
    if close_location >= 0.67:
        bullish_score += 8
    if breakout_up:
        bullish_score += 12

    if row["ema_9"] < row["ema_21"]:
        bearish_score += 20
    if row["macd_hist"] < 0:
        bearish_score += 18
    if 28 <= row["rsi"] <= 46:
        bearish_score += 16
    if percent_change <= -0.18:
        bearish_score += min(abs(percent_change) * 50, 15)
    if breadth_ratio < -0.08:
        bearish_score += min(abs(breadth_ratio) * 100, 15)
    if close_location <= 0.33:
        bearish_score += 8
    if breakout_down:
        bearish_score += 12

    strike = round_to_strike(price)
    if bullish_score >= 60 and bullish_score > bearish_score:
        return {
            "status": "BUY",
            "option_type": "CE",
            "strike": strike,
            "score": round(bullish_score, 2),
            "stop_loss": round(price - max(1.2 * atr, 18), 2),
            "target": round(price + max(1.8 * atr, 28), 2),
            "reason": (
                f"Candle close confirmation for NIFTY {strike} CE. "
                f"EMA9 above EMA21, MACD positive, RSI {row['rsi']:.1f}, "
                f"breadth {breadth_ratio:.2f}, pct {percent_change:.2f}%."
            ),
        }

    if bearish_score >= 60 and bearish_score > bullish_score:
        return {
            "status": "BUY",
            "option_type": "PE",
            "strike": strike,
            "score": round(bearish_score, 2),
            "stop_loss": round(price + max(1.2 * atr, 18), 2),
            "target": round(price - max(1.8 * atr, 28), 2),
            "reason": (
                f"Candle close confirmation for NIFTY {strike} PE. "
                f"EMA9 below EMA21, MACD negative, RSI {row['rsi']:.1f}, "
                f"breadth {breadth_ratio:.2f}, pct {percent_change:.2f}%."
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
            should_alert=should_send_hourly_alert(state, now_ist(), hourly_interval_minutes),
            hourly_summary=should_send_hourly_alert(state, now_ist(), hourly_interval_minutes),
        )
        return result, state

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
            should_alert=send_hold or hourly_summary,
            hourly_summary=hourly_summary,
        )
        return result, state

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
            should_alert=hourly_summary,
            hourly_summary=hourly_summary,
        )
        return result, state

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
            should_alert=hourly_summary,
            hourly_summary=hourly_summary,
        )
        return result, state

    latest = enriched.iloc[-1]
    bucket = latest["time"].isoformat()
    entry = build_entry_signal_from_row(latest)

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
            should_alert=True,
        )
        return result, state

    result = TrackerResult(
        status="WAIT",
        title="NIFTY Signal",
        spot_price=snapshot.last,
        time=snapshot.timestamp,
        reason=(
            f"Wait. Latest candle score not strong enough for a new entry. "
            f"EMA9 {latest['ema_9']:.2f}, EMA21 {latest['ema_21']:.2f}, "
            f"RSI {latest['rsi']:.1f}, MACD hist {latest['macd_hist']:.2f}."
        ),
        should_alert=hourly_summary,
        hourly_summary=hourly_summary,
    )
    return result, state


def format_result(result: TrackerResult) -> str:
    lines = [
        f"NIFTY Status: {result.status}",
        f"Time: {result.time:%Y-%m-%d %H:%M %Z}",
        f"Spot price: {result.spot_price:.2f}",
    ]
    if result.option_type and result.strike:
        lines.append(f"Option idea: NIFTY {result.strike} {result.option_type}")
    if result.score:
        lines.append(f"Score: {result.score:.2f}")
    lines.append(f"Reason: {result.reason}")
    if result.stop_loss is not None:
        lines.append(f"Underlying stop-loss: {result.stop_loss:.2f}")
    if result.target is not None:
        lines.append(f"Underlying target: {result.target:.2f}")
    if result.pnl_points is not None:
        lines.append(f"Running P&L (underlying points): {result.pnl_points:.2f}")
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


def run_backtest(range_: str = "30d") -> str:
    candles = enrich_candles(fetch_yahoo_candles(range_=range_))
    active: dict | None = None
    trades: list[dict] = []

    for _, row in candles.iterrows():
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
                        "entry_price": active["entry_price"],
                        "exit_price": price,
                        "pnl_points": round(pnl, 2),
                    }
                )
                active = None
                continue

        if not active:
            entry = build_entry_signal_from_row(row)
            if entry:
                active = {
                    "entry_time": row["time"],
                    "option_type": entry["option_type"],
                    "strike": entry["strike"],
                    "entry_price": price,
                    "stop_loss": entry["stop_loss"],
                    "target": entry["target"],
                }

    total_trades = len(trades)
    wins = sum(1 for trade in trades if trade["pnl_points"] > 0)
    losses = sum(1 for trade in trades if trade["pnl_points"] <= 0)
    total_pnl = round(sum(trade["pnl_points"] for trade in trades), 2)
    avg_pnl = round(total_pnl / total_trades, 2) if total_trades else 0
    win_rate = round((wins / total_trades) * 100, 2) if total_trades else 0

    lines = [
        "Backtest Summary",
        f"Range: {range_}",
        f"Trades: {total_trades}",
        f"Wins: {wins}",
        f"Losses: {losses}",
        f"Win rate: {win_rate:.2f}%",
        f"Total P&L (underlying points): {total_pnl:.2f}",
        f"Average P&L per trade: {avg_pnl:.2f}",
    ]
    if trades:
        last_trade = trades[-1]
        lines.append(
            "Last trade: "
            f"{last_trade['option_type']} {last_trade['strike']} "
            f"{last_trade['entry_time']:%Y-%m-%d %H:%M} -> "
            f"{last_trade['exit_time']:%Y-%m-%d %H:%M}, "
            f"PnL {last_trade['pnl_points']:.2f}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY live option tracker")
    parser.add_argument("--once", action="store_true", help="Run a single live cycle and exit.")
    parser.add_argument("--backtest", action="store_true", help="Run historical backtest instead of live tracking.")
    parser.add_argument("--backtest-range", default="30d", help="Yahoo range for backtest, for example 30d or 60d.")
    parser.add_argument("--ignore-market-hours", action="store_true", help="Run even outside NSE market hours.")
    parser.add_argument("--max-data-age-minutes", type=int, default=15)
    parser.add_argument("--hold-update-minutes", type=int, default=DEFAULT_HOLD_INTERVAL_MINUTES)
    parser.add_argument("--hourly-summary-minutes", type=int, default=DEFAULT_HOURLY_INTERVAL_MINUTES)
    args = parser.parse_args()

    load_dotenv()

    if args.backtest:
        print(run_backtest(range_=args.backtest_range))
        return

    state = load_state()
    if not args.ignore_market_hours and not is_market_open():
        result = TrackerResult(
            status="WAIT",
            title="NIFTY Signal",
            spot_price=0,
            time=now_ist(),
            reason="Market is closed. Waiting for next NSE session.",
            should_alert=False,
        )
        print(format_result(result))
        return

    result, updated_state = run_live_cycle(
        state=state,
        max_data_age_minutes=args.max_data_age_minutes,
        hold_interval_minutes=args.hold_update_minutes,
        hourly_interval_minutes=args.hourly_summary_minutes,
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


if __name__ == "__main__":
    main()
