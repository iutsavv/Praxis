"""Multi-timeframe confluence checks for Stage 2 signal filtering."""

from __future__ import annotations

import os
import sqlite3
from typing import Any


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "database", "trading.db")
TIMEFRAMES = ("5m", "15m", "1h", "1d")
MAIN_TIMEFRAMES = ("5m", "15m", "1h")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_indicator(conn: sqlite3.Connection, symbol: str, interval: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT rsi, macd_histogram, ema_20, ema_50, timestamp
        FROM indicators
        WHERE symbol = ? AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, interval),
    ).fetchone()


def _timeframe_bias(row: sqlite3.Row | None) -> str:
    if row is None:
        return "NEUTRAL"

    ema_20 = row["ema_20"]
    ema_50 = row["ema_50"]
    macd_histogram = row["macd_histogram"]
    rsi = row["rsi"]
    if None in (ema_20, ema_50, macd_histogram, rsi):
        return "NEUTRAL"

    if ema_20 > ema_50 and macd_histogram > 0 and 40 <= rsi <= 65:
        return "BULLISH"
    if ema_20 < ema_50 and macd_histogram < 0 and 35 <= rsi <= 60:
        return "BEARISH"
    return "NEUTRAL"


def _direction_from_bias(bias: str) -> str:
    if bias == "BULLISH":
        return "BUY"
    if bias == "BEARISH":
        return "SELL"
    return "SKIP"


def get_timeframe_confluence(symbol: str) -> dict[str, Any]:
    """Read latest indicators and determine multi-timeframe agreement."""
    with _get_connection() as conn:
        states = {
            timeframe: _timeframe_bias(_latest_indicator(conn, symbol, timeframe))
            for timeframe in TIMEFRAMES
        }

    main_states = [states[timeframe] for timeframe in MAIN_TIMEFRAMES]
    bullish_main = main_states.count("BULLISH")
    bearish_main = main_states.count("BEARISH")

    if bullish_main >= 2 and bullish_main >= bearish_main:
        dominant = "BULLISH"
        agree_main = bullish_main
    elif bearish_main >= 2:
        dominant = "BEARISH"
        agree_main = bearish_main
    else:
        dominant = "NEUTRAL"
        agree_main = max(bullish_main, bearish_main)

    all_agree = list(states.values()).count(dominant) if dominant != "NEUTRAL" else 0
    confluence_score = int(round((all_agree / len(TIMEFRAMES)) * 100))
    if agree_main >= 3:
        confluence_level = "HIGH"
    elif agree_main >= 2:
        confluence_level = "MEDIUM"
    elif agree_main >= 1:
        confluence_level = "LOW"
    else:
        confluence_level = "LOW"

    trade_direction = _direction_from_bias(dominant) if agree_main >= 2 else "SKIP"
    reasoning = (
        f"{agree_main} of 3 main timeframes {dominant.lower()}"
        if trade_direction != "SKIP"
        else "fewer than 2 of 3 main timeframes agree"
    )

    return {
        **states,
        "confluence_score": confluence_score,
        "confluence_level": confluence_level,
        "trade_direction": trade_direction,
        "reasoning": reasoning,
    }


def should_trade_confluence(symbol: str) -> bool:
    """Return True when at least 2 of 3 main timeframes agree."""
    return get_timeframe_confluence(symbol)["trade_direction"] != "SKIP"
