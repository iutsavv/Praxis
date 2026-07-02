"""
indicator_engine.py - Technical indicator engine for the AI Paper Trading Agent.

Single responsibility: read raw 15-minute candles from database/trading.db,
compute technical indicators, and write them to the indicators table.

No network calls. No signal scoring. No trading logic.

Usage:
    python analysis/indicator_engine.py
"""

from __future__ import annotations

import math
import numpy as np
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any

import pandas as pd

try:
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator, MACD

    HAS_TA = True
except ImportError:
    HAS_TA = False


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "database", "trading.db")

INTERVAL = "15m"
NIFTY_SYMBOL = "NSEI"
WARMUP_CANDLES = 250
MAX_VALIDATION_FAILURE_RATE = 0.20


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SymbolRunResult:
    symbol: str
    candles_seen: int
    indicators_inserted: int
    validation_failures: int
    stopped: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        print("        Run  python database/init_db.py  first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_active_symbols(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol"
    ).fetchall()
    symbols = [row["symbol"] for row in rows]

    if NIFTY_SYMBOL not in symbols:
        symbols.append(NIFTY_SYMBOL)

    return symbols


def get_latest_indicator_timestamp(conn: sqlite3.Connection, symbol: str) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(timestamp) AS latest_ts
        FROM indicators
        WHERE symbol = ? AND interval = ?
        """,
        (symbol, INTERVAL),
    ).fetchone()
    return row["latest_ts"] if row and row["latest_ts"] else None


def get_missing_indicator_timestamps(conn: sqlite3.Connection, symbol: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT c.timestamp
        FROM candles c
        LEFT JOIN indicators i
          ON i.symbol = c.symbol
         AND i.interval = c.interval
         AND i.timestamp = c.timestamp
        WHERE c.symbol = ?
          AND c.interval = ?
          AND i.id IS NULL
        ORDER BY c.timestamp ASC
        """,
        (symbol, INTERVAL),
    ).fetchall()
    return {row["timestamp"] for row in rows}


def get_candle_count(conn: sqlite3.Connection, symbol: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS candle_count
        FROM candles
        WHERE symbol = ? AND interval = ?
        """,
        (symbol, INTERVAL),
    ).fetchone()
    return int(row["candle_count"])


def get_candles_for_calculation(
    conn: sqlite3.Connection,
    symbol: str,
    latest_indicator_ts: str | None,
    has_missing_indicator_rows: bool = False,
) -> pd.DataFrame:
    if latest_indicator_ts is None or has_missing_indicator_rows:
        rows = conn.execute(
            """
            SELECT id, symbol, interval, open, high, low, close, volume, timestamp
            FROM candles
            WHERE symbol = ? AND interval = ?
            ORDER BY timestamp ASC
            """,
            (symbol, INTERVAL),
        ).fetchall()
    else:
        prior_rows = conn.execute(
            """
            SELECT id, symbol, interval, open, high, low, close, volume, timestamp
            FROM candles
            WHERE symbol = ? AND interval = ? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, INTERVAL, latest_indicator_ts, WARMUP_CANDLES),
        ).fetchall()
        new_rows = conn.execute(
            """
            SELECT id, symbol, interval, open, high, low, close, volume, timestamp
            FROM candles
            WHERE symbol = ? AND interval = ? AND timestamp > ?
            ORDER BY timestamp ASC
            """,
            (symbol, INTERVAL, latest_indicator_ts),
        ).fetchall()
        rows = list(reversed(prior_rows)) + list(new_rows)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(row) for row in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates(subset=["id"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def _compute_rsi_fallback(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff().fillna(0)
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))

    rsi = rsi.where(avg_loss != 0, 100)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50)
    rsi = rsi.fillna(50)
    return rsi.astype(float)


def _compute_vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    traded_value = typical_price * df["volume"]
    session = df["timestamp"].dt.date

    cumulative_value = traded_value.groupby(session).cumsum()
    cumulative_volume = df["volume"].groupby(session).cumsum()
    return cumulative_value / cumulative_volume.replace(0, pd.NA)


def _compute_volume_ratio(df: pd.DataFrame) -> pd.Series:
    work = df[["timestamp", "volume"]].copy()
    work["trade_date"] = work["timestamp"].dt.date
    work["time_of_day"] = work["timestamp"].dt.strftime("%H:%M:%S")

    ratios = pd.Series(index=work.index, dtype="float64")
    for _, group in work.groupby("time_of_day", sort=False):
        average_same_time_volume = (
            group["volume"]
            .shift(1)
            .rolling(window=10, min_periods=1)
            .mean()
        )
        # Keep the result strictly float64. pandas 3 rejects assigning an
        # object array containing pd.NA into this float series.
        denominator = average_same_time_volume.astype(float).replace(0.0, np.nan)
        ratios.loc[group.index] = (group["volume"].astype(float) / denominator).astype(float)

    return ratios


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    result = df.copy()
    close = result["close"].astype(float)

    if HAS_TA:
        result["rsi"] = RSIIndicator(close=close, window=14, fillna=True).rsi()
        macd_indicator = MACD(
            close=close,
            window_slow=26,
            window_fast=12,
            window_sign=9,
            fillna=True,
        )
        result["macd"] = macd_indicator.macd()
        result["macd_signal"] = macd_indicator.macd_signal()
        result["macd_histogram"] = macd_indicator.macd_diff()
        result["ema_20"] = EMAIndicator(close=close, window=20, fillna=True).ema_indicator()
        result["ema_50"] = EMAIndicator(close=close, window=50, fillna=True).ema_indicator()
    else:
        result["rsi"] = _compute_rsi_fallback(close, period=14)
        result["ema_20"] = close.ewm(span=20, adjust=False, min_periods=1).mean()
        result["ema_50"] = close.ewm(span=50, adjust=False, min_periods=1).mean()
        ema_12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema_26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        result["macd"] = ema_12 - ema_26
        result["macd_signal"] = result["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
        result["macd_histogram"] = result["macd"] - result["macd_signal"]

    result["vwap"] = _compute_vwap(result)
    result["volume_ratio"] = _compute_volume_ratio(result)

    return result


# ---------------------------------------------------------------------------
# Validation and storage
# ---------------------------------------------------------------------------

def _clean_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return numeric


def validate_indicator_row(row: pd.Series, require_vwap: bool = True) -> tuple[bool, str]:
    rsi = _clean_float(row["rsi"])
    close = _clean_float(row["close"])
    vwap = _clean_float(row["vwap"])

    if rsi is None or not 0 <= rsi <= 100:
        return False, f"RSI out of range: {rsi!r}"

    if close is None or close <= 0:
        return False, f"invalid candle close: {close!r}"

    if require_vwap and (vwap is None or vwap <= 0):
        return False, f"invalid VWAP: {vwap!r}"

    if vwap is not None and (vwap > close * 10 or vwap < close * 0.1):
        return False, f"VWAP wildly off close: vwap={vwap:.4f}, close={close:.4f}"

    return True, ""


def insert_indicator_rows(
    conn: sqlite3.Connection,
    symbol: str,
    df: pd.DataFrame,
    latest_indicator_ts: str | None,
    missing_indicator_timestamps: set[str] | None = None,
) -> tuple[int, int, bool]:
    missing_indicator_timestamps = missing_indicator_timestamps or set()

    if latest_indicator_ts is not None:
        timestamp_text = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        rows_to_insert = df[
            (df["timestamp"] > pd.Timestamp(latest_indicator_ts))
            | (timestamp_text.isin(missing_indicator_timestamps))
        ].copy()
    else:
        rows_to_insert = df.copy()

    if rows_to_insert.empty:
        return 0, 0, False

    cursor = conn.cursor()
    inserted = 0
    validation_failures = 0

    for _, row in rows_to_insert.iterrows():
        is_valid, reason = validate_indicator_row(row, require_vwap=symbol != NIFTY_SYMBOL)
        if not is_valid:
            validation_failures += 1
            print(
                f"  [WARN] {symbol:<12} {row['timestamp']} skipped validation: {reason}"
            )

            failure_rate = validation_failures / len(rows_to_insert)
            if failure_rate > MAX_VALIDATION_FAILURE_RATE:
                print(
                    f"  [ERROR] {symbol}: validation failures exceeded "
                    f"{MAX_VALIDATION_FAILURE_RATE:.0%}; stopping this symbol."
                )
                return inserted, validation_failures, True

            continue

        timestamp = row["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT OR IGNORE INTO indicators (
                candle_id,
                symbol,
                interval,
                rsi,
                macd,
                macd_signal,
                macd_histogram,
                ema_20,
                ema_50,
                vwap,
                volume_ratio,
                timestamp
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1
                FROM indicators
                WHERE symbol = ? AND interval = ? AND timestamp = ?
            )
            """,
            (
                int(row["id"]),
                symbol,
                INTERVAL,
                _clean_float(row["rsi"]),
                _clean_float(row["macd"]),
                _clean_float(row["macd_signal"]),
                _clean_float(row["macd_histogram"]),
                _clean_float(row["ema_20"]),
                _clean_float(row["ema_50"]),
                _clean_float(row["vwap"]),
                _clean_float(row["volume_ratio"]),
                timestamp,
                symbol,
                INTERVAL,
                timestamp,
            ),
        )
        inserted += cursor.rowcount

    conn.commit()
    return inserted, validation_failures, False


# ---------------------------------------------------------------------------
# Runner and reporting
# ---------------------------------------------------------------------------

def process_symbol(conn: sqlite3.Connection, symbol: str) -> SymbolRunResult:
    total_candles = get_candle_count(conn, symbol)
    if total_candles == 0:
        return SymbolRunResult(
            symbol=symbol,
            candles_seen=0,
            indicators_inserted=0,
            validation_failures=0,
            message="no 15m candles found",
        )

    latest_indicator_ts = get_latest_indicator_timestamp(conn, symbol)
    missing_indicator_timestamps = get_missing_indicator_timestamps(conn, symbol)
    candles = get_candles_for_calculation(
        conn,
        symbol,
        latest_indicator_ts,
        has_missing_indicator_rows=bool(missing_indicator_timestamps),
    )
    if candles.empty:
        return SymbolRunResult(
            symbol=symbol,
            candles_seen=total_candles,
            indicators_inserted=0,
            validation_failures=0,
            message="no new candles",
        )

    computed = compute_indicators(candles)
    inserted, validation_failures, stopped = insert_indicator_rows(
        conn,
        symbol,
        computed,
        latest_indicator_ts,
        missing_indicator_timestamps,
    )

    return SymbolRunResult(
        symbol=symbol,
        candles_seen=total_candles,
        indicators_inserted=inserted,
        validation_failures=validation_failures,
        stopped=stopped,
    )


def print_run_summary(results: list[SymbolRunResult]) -> None:
    total_failures = sum(result.validation_failures for result in results)

    print("── INDICATOR ENGINE RUN ──────────────────────")
    for result in results:
        line = (
            f"{result.symbol:<12} {result.candles_seen:>6,} candles → "
            f"{result.indicators_inserted:>6,} indicators computed"
        )
        if result.stopped:
            line += "  [STOPPED]"
        elif result.message:
            line += f"  ({result.message})"
        print(line)

    print("──────────────────────────────────────────────")
    print(f"Total: {len(results)} symbols processed, {total_failures} validation failures")


def print_latest_samples(conn: sqlite3.Connection, sample_count: int = 3) -> None:
    symbols = [
        row["symbol"]
        for row in conn.execute(
            """
            SELECT symbol
            FROM indicators
            GROUP BY symbol
            ORDER BY CASE symbol
                WHEN 'RELIANCE' THEN 0
                WHEN 'ICICIBANK' THEN 1
                WHEN 'NSEI' THEN 2
                ELSE 3
            END, symbol
            LIMIT ?
            """,
            (sample_count,),
        ).fetchall()
    ]

    print("\nLatest indicator samples:")
    if not symbols:
        print("  (no indicator rows found)")
        return

    for symbol in symbols:
        row = conn.execute(
            """
            SELECT symbol, timestamp, rsi, macd, macd_signal, macd_histogram,
                   ema_20, ema_50, vwap, volume_ratio
            FROM indicators
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        print(
            f"  {row['symbol']:<12} {row['timestamp']}  "
            f"RSI={row['rsi']:.2f}  MACD={row['macd']:.4f}  "
            f"Signal={row['macd_signal']:.4f}  Hist={row['macd_histogram']:.4f}  "
            f"VWAP={row['vwap']:.2f}  VolRatio={_clean_float(row['volume_ratio'])}"
        )


def run_indicator_engine() -> list[SymbolRunResult]:
    conn = _get_connection()
    try:
        symbols = get_active_symbols(conn)
        results = [process_symbol(conn, symbol) for symbol in symbols]
        print_run_summary(results)
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_indicator_engine()

    sample_conn = _get_connection()
    try:
        print_latest_samples(sample_conn, sample_count=3)
    finally:
        sample_conn.close()
