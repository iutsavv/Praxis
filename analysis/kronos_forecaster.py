"""Optional Kronos price forecasting integration.

This module keeps the heavy Kronos/PyTorch dependency out of the normal
paper-trading path unless ENABLE_KRONOS=1 is set.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
KRONOS_ROOT = PROJECT_ROOT / "Kronos"

DEFAULT_MODEL_ID = "NeoQuasar/Kronos-mini"
DEFAULT_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-2k"
DEFAULT_INTERVAL = "1d"
DEFAULT_LOOKBACK = 400
DEFAULT_PRED_LEN = 15
DEFAULT_SAMPLE_COUNT = 1

_PREDICTOR_CACHE: dict[str, Any] = {}


@dataclass
class KronosForecast:
    symbol: str
    enabled: bool
    available: bool
    interval: str = DEFAULT_INTERVAL
    lookback: int = DEFAULT_LOOKBACK
    pred_len: int = DEFAULT_PRED_LEN
    model_name: str = DEFAULT_MODEL_ID
    forecast_direction: str = "NEUTRAL"
    last_close: float | None = None
    predicted_close: float | None = None
    expected_return_pct: float = 0.0
    score_adjustment: float = 0.0
    confirms_signal: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def kronos_enabled() -> bool:
    return os.getenv("ENABLE_KRONOS", "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_kronos_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kronos_forecasts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT NOT NULL,
            scan_time           TEXT,
            interval            TEXT NOT NULL,
            lookback            INTEGER NOT NULL,
            pred_len            INTEGER NOT NULL,
            model_name          TEXT NOT NULL,
            last_close          REAL,
            predicted_close     REAL,
            expected_return_pct REAL,
            forecast_direction  TEXT,
            score_adjustment    REAL DEFAULT 0,
            confirms_signal     INTEGER DEFAULT 0,
            message             TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _setting_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _load_kronos_predictor() -> Any:
    model_id = os.getenv("KRONOS_MODEL_ID", DEFAULT_MODEL_ID)
    tokenizer_id = os.getenv("KRONOS_TOKENIZER_ID", DEFAULT_TOKENIZER_ID)
    device = os.getenv("KRONOS_DEVICE", "cpu")
    max_context = _setting_int("KRONOS_MAX_CONTEXT", 2048 if "mini" in model_id.lower() else 512)
    cache_key = f"{model_id}|{tokenizer_id}|{device}|{max_context}"

    if cache_key in _PREDICTOR_CACHE:
        return _PREDICTOR_CACHE[cache_key]

    if not KRONOS_ROOT.exists():
        raise RuntimeError(f"Kronos repo not found at {KRONOS_ROOT}")

    if str(KRONOS_ROOT) not in sys.path:
        sys.path.insert(0, str(KRONOS_ROOT))

    from model import Kronos, KronosPredictor, KronosTokenizer

    tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
    model = Kronos.from_pretrained(model_id)
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=max_context)
    _PREDICTOR_CACHE[cache_key] = predictor
    return predictor


def _read_recent_candles(symbol: str, interval: str, lookback: int) -> pd.DataFrame:
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT open, high, low, close, volume, timestamp
            FROM candles
            WHERE UPPER(symbol) = UPPER(?) AND interval = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, interval, lookback),
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(row) for row in reversed(rows)])
    df["timestamps"] = pd.to_datetime(df["timestamp"])
    df["amount"] = df["volume"].astype(float) * df[["open", "high", "low", "close"]].mean(axis=1)
    return df


def _future_timestamps(history: pd.Series, pred_len: int, interval: str) -> pd.Series:
    if history.empty:
        return pd.Series(dtype="datetime64[ns]")

    last_ts = pd.Timestamp(history.iloc[-1])
    if interval == "1d":
        future = pd.bdate_range(start=last_ts + pd.Timedelta(days=1), periods=pred_len)
    elif interval == "1h":
        future = pd.date_range(start=last_ts + pd.Timedelta(hours=1), periods=pred_len, freq="1h")
    elif interval == "5m":
        future = pd.date_range(start=last_ts + pd.Timedelta(minutes=5), periods=pred_len, freq="5min")
    else:
        future = pd.date_range(start=last_ts + pd.Timedelta(minutes=15), periods=pred_len, freq="15min")
    return pd.Series(future, name="timestamps")


def _direction_from_return(expected_return_pct: float) -> str:
    if expected_return_pct > 0.25:
        return "BUY"
    if expected_return_pct < -0.25:
        return "SELL"
    return "NEUTRAL"


def _score_adjustment(expected_return_pct: float, signal_direction: str) -> tuple[float, bool]:
    forecast_direction = _direction_from_return(expected_return_pct)
    if forecast_direction == "NEUTRAL" or signal_direction == "HOLD":
        return 0.0, False

    magnitude = min(abs(expected_return_pct) * 2.0, 15.0)
    confirms_signal = forecast_direction == signal_direction
    return (magnitude if confirms_signal else -magnitude), confirms_signal


def forecast_symbol(
    symbol: str,
    signal_direction: str,
    scan_time: str = "",
    interval: str | None = None,
    lookback: int | None = None,
    pred_len: int | None = None,
) -> KronosForecast:
    active_interval = interval or os.getenv("KRONOS_INTERVAL", DEFAULT_INTERVAL)
    active_lookback = lookback or _setting_int("KRONOS_LOOKBACK", DEFAULT_LOOKBACK)
    active_pred_len = pred_len or _setting_int("KRONOS_PRED_LEN", DEFAULT_PRED_LEN)
    model_name = os.getenv("KRONOS_MODEL_ID", DEFAULT_MODEL_ID)

    forecast = KronosForecast(
        symbol=symbol,
        enabled=kronos_enabled(),
        available=False,
        interval=active_interval,
        lookback=active_lookback,
        pred_len=active_pred_len,
        model_name=model_name,
    )

    if not forecast.enabled:
        forecast.message = "Kronos disabled. Set ENABLE_KRONOS=1 to enable forecasts."
        return forecast

    candles = _read_recent_candles(symbol, active_interval, active_lookback)
    if len(candles) < min(60, active_lookback):
        forecast.message = f"Not enough {active_interval} candles for Kronos forecast."
        return forecast

    try:
        predictor = _load_kronos_predictor()
        x_df = candles[["open", "high", "low", "close", "volume", "amount"]]
        x_timestamp = candles["timestamps"]
        y_timestamp = _future_timestamps(x_timestamp, active_pred_len, active_interval)
        pred_df = predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=active_pred_len,
            T=float(os.getenv("KRONOS_TEMPERATURE", "1.0")),
            top_p=float(os.getenv("KRONOS_TOP_P", "0.9")),
            sample_count=_setting_int("KRONOS_SAMPLE_COUNT", DEFAULT_SAMPLE_COUNT),
            verbose=False,
        )
    except Exception as exc:
        forecast.message = f"Kronos forecast failed: {exc}"
        return forecast

    last_close = float(candles["close"].iloc[-1])
    predicted_close = float(pred_df["close"].iloc[-1])
    expected_return_pct = ((predicted_close - last_close) / last_close) * 100 if last_close else 0.0
    score_adjustment, confirms_signal = _score_adjustment(expected_return_pct, signal_direction)

    forecast.available = True
    forecast.last_close = round(last_close, 4)
    forecast.predicted_close = round(predicted_close, 4)
    forecast.expected_return_pct = round(expected_return_pct, 4)
    forecast.forecast_direction = _direction_from_return(expected_return_pct)
    forecast.score_adjustment = round(score_adjustment, 2)
    forecast.confirms_signal = confirms_signal
    forecast.message = "Kronos forecast generated."
    return forecast


def write_kronos_forecast(forecast: KronosForecast, scan_time: str = "") -> None:
    if not forecast.enabled:
        return

    with _get_connection() as conn:
        ensure_kronos_schema(conn)
        conn.execute(
            """
            INSERT INTO kronos_forecasts (
                symbol, scan_time, interval, lookback, pred_len, model_name,
                last_close, predicted_close, expected_return_pct,
                forecast_direction, score_adjustment, confirms_signal, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forecast.symbol,
                scan_time,
                forecast.interval,
                forecast.lookback,
                forecast.pred_len,
                forecast.model_name,
                forecast.last_close,
                forecast.predicted_close,
                forecast.expected_return_pct,
                forecast.forecast_direction,
                forecast.score_adjustment,
                1 if forecast.confirms_signal else 0,
                forecast.message,
            ),
        )
        conn.commit()


def apply_kronos_adjustment(
    symbol: str,
    signal_direction: str,
    base_score: float,
    scan_time: str = "",
) -> tuple[float, dict[str, Any]]:
    forecast = forecast_symbol(symbol, signal_direction, scan_time=scan_time)
    write_kronos_forecast(forecast, scan_time=scan_time)

    adjusted_score = base_score
    if forecast.available:
        adjusted_score = max(0.0, min(100.0, round(base_score + forecast.score_adjustment, 2)))

    return adjusted_score, forecast.to_dict()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run one Kronos forecast from local candles.")
    parser.add_argument("symbol", help="Stock symbol, e.g. RELIANCE")
    parser.add_argument("--direction", default="BUY", choices=["BUY", "SELL", "HOLD"])
    parser.add_argument("--interval", default=None)
    parser.add_argument("--pred-len", type=int, default=None)
    args = parser.parse_args()

    result = forecast_symbol(
        args.symbol.upper(),
        args.direction,
        scan_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        interval=args.interval,
        pred_len=args.pred_len,
    )
    print(json.dumps(result.to_dict(), indent=2))
