"""Candlestick pattern detection from stored NSE candle data."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"
PatternResult = tuple[bool, float, str]
PatternFn = Callable[[pd.DataFrame], PatternResult]


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_patterns_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS patterns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            interval        TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            pattern_name    TEXT NOT NULL,
            pattern_type    TEXT NOT NULL,
            direction       TEXT NOT NULL,
            strength        REAL DEFAULT 50,
            volume_confirmed INTEGER DEFAULT 0,
            candles_used    INTEGER DEFAULT 1,
            detected_at     TEXT DEFAULT (datetime('now')),
            UNIQUE(symbol, interval, timestamp, pattern_name)
        )
        """
    )
    conn.commit()


def _body(c: Any) -> float:
    return abs(float(c.close) - float(c.open))


def _range(c: Any) -> float:
    return max(0.0, float(c.high) - float(c.low))


def _upper_wick(c: Any) -> float:
    return float(c.high) - max(float(c.open), float(c.close))


def _lower_wick(c: Any) -> float:
    return min(float(c.open), float(c.close)) - float(c.low)


def _is_green(c: Any) -> bool:
    return float(c.close) > float(c.open)


def _is_red(c: Any) -> bool:
    return float(c.close) < float(c.open)


def _prior_trend(candles: pd.DataFrame, lookback: int = 3) -> str:
    if len(candles) < lookback + 1:
        return "NEUTRAL"
    previous = candles.iloc[-lookback - 1 : -1]
    latest_close = float(candles.iloc[-1].close)
    if float(previous["close"].mean()) > latest_close:
        return "DOWN"
    if float(previous["close"].mean()) < latest_close:
        return "UP"
    return "NEUTRAL"


def _avg_body(candles: pd.DataFrame, lookback: int = 10) -> float:
    recent = candles.tail(lookback)
    if recent.empty:
        return 0.0
    return float((recent["close"].astype(float) - recent["open"].astype(float)).abs().mean())


def _volume_confirmed(candles: pd.DataFrame, lookback: int = 10) -> bool:
    if len(candles) < 2:
        return False
    avg_volume = float(candles.tail(lookback)["volume"].astype(float).mean())
    return avg_volume > 0 and float(candles.iloc[-1].volume) >= avg_volume


def detect_doji(candles: pd.DataFrame) -> PatternResult:
    c = candles.iloc[-1]
    total_range = _range(c)
    if total_range == 0:
        return False, 0, "NEUTRAL"
    return _body(c) / total_range < 0.1, 50, "NEUTRAL"


def detect_hammer(candles: pd.DataFrame) -> PatternResult:
    c = candles.iloc[-1]
    body = _body(c)
    if body == 0:
        return False, 0, "BULLISH"
    is_hammer = _lower_wick(c) >= 2 * body and _upper_wick(c) <= 0.3 * body
    strength = 70 if is_hammer and _prior_trend(candles) == "DOWN" else 50
    return is_hammer, strength, "BULLISH"


def detect_shooting_star(candles: pd.DataFrame) -> PatternResult:
    c = candles.iloc[-1]
    body = _body(c)
    if body == 0:
        return False, 0, "BEARISH"
    detected = _upper_wick(c) >= 2 * body and _lower_wick(c) <= 0.3 * body
    strength = 70 if detected and _prior_trend(candles) == "UP" else 50
    return detected, strength, "BEARISH"


def detect_marubozu_bull(candles: pd.DataFrame) -> PatternResult:
    c = candles.iloc[-1]
    total_range = _range(c)
    if total_range == 0:
        return False, 0, "BULLISH"
    detected = _is_green(c) and _body(c) / total_range >= 0.85 and _body(c) >= _avg_body(candles) * 1.2
    return detected, 75 if _volume_confirmed(candles) else 65, "BULLISH"


def detect_marubozu_bear(candles: pd.DataFrame) -> PatternResult:
    c = candles.iloc[-1]
    total_range = _range(c)
    if total_range == 0:
        return False, 0, "BEARISH"
    detected = _is_red(c) and _body(c) / total_range >= 0.85 and _body(c) >= _avg_body(candles) * 1.2
    return detected, 75 if _volume_confirmed(candles) else 65, "BEARISH"


def detect_spinning_top(candles: pd.DataFrame) -> PatternResult:
    c = candles.iloc[-1]
    total_range = _range(c)
    if total_range == 0:
        return False, 0, "NEUTRAL"
    body_ratio = _body(c) / total_range
    wick_balance = abs(_upper_wick(c) - _lower_wick(c)) / total_range
    return 0.1 <= body_ratio <= 0.35 and wick_balance <= 0.25, 50, "NEUTRAL"


def detect_bullish_engulfing(candles: pd.DataFrame) -> PatternResult:
    prev, curr = candles.iloc[-2], candles.iloc[-1]
    detected = _is_red(prev) and _is_green(curr) and float(curr.open) < float(prev.close) and float(curr.close) > float(prev.open)
    vol_confirm = float(curr.volume) > float(prev.volume)
    return detected, 80 if detected and vol_confirm else 65, "BULLISH"


def detect_bearish_engulfing(candles: pd.DataFrame) -> PatternResult:
    prev, curr = candles.iloc[-2], candles.iloc[-1]
    detected = _is_green(prev) and _is_red(curr) and float(curr.open) > float(prev.close) and float(curr.close) < float(prev.open)
    vol_confirm = float(curr.volume) > float(prev.volume)
    return detected, 80 if detected and vol_confirm else 65, "BEARISH"


def detect_bullish_harami(candles: pd.DataFrame) -> PatternResult:
    prev, curr = candles.iloc[-2], candles.iloc[-1]
    detected = _is_red(prev) and _is_green(curr) and float(curr.open) > float(prev.close) and float(curr.close) < float(prev.open)
    return detected, 65, "BULLISH"


def detect_bearish_harami(candles: pd.DataFrame) -> PatternResult:
    prev, curr = candles.iloc[-2], candles.iloc[-1]
    detected = _is_green(prev) and _is_red(curr) and float(curr.open) < float(prev.close) and float(curr.close) > float(prev.open)
    return detected, 65, "BEARISH"


def detect_tweezer_bottom(candles: pd.DataFrame) -> PatternResult:
    prev, curr = candles.iloc[-2], candles.iloc[-1]
    tolerance = max(float(prev.low), 1.0) * 0.002
    detected = _is_red(prev) and _is_green(curr) and abs(float(prev.low) - float(curr.low)) <= tolerance
    return detected, 70 if _volume_confirmed(candles) else 60, "BULLISH"


def detect_tweezer_top(candles: pd.DataFrame) -> PatternResult:
    prev, curr = candles.iloc[-2], candles.iloc[-1]
    tolerance = max(float(prev.high), 1.0) * 0.002
    detected = _is_green(prev) and _is_red(curr) and abs(float(prev.high) - float(curr.high)) <= tolerance
    return detected, 70 if _volume_confirmed(candles) else 60, "BEARISH"


def detect_morning_star(candles: pd.DataFrame) -> PatternResult:
    a, b, c = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
    midpoint = (float(a.open) + float(a.close)) / 2
    detected = _is_red(a) and _body(b) < _body(a) * 0.45 and _is_green(c) and float(c.close) > midpoint
    return detected, 80 if _volume_confirmed(candles) else 70, "BULLISH"


def detect_evening_star(candles: pd.DataFrame) -> PatternResult:
    a, b, c = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
    midpoint = (float(a.open) + float(a.close)) / 2
    detected = _is_green(a) and _body(b) < _body(a) * 0.45 and _is_red(c) and float(c.close) < midpoint
    return detected, 80 if _volume_confirmed(candles) else 70, "BEARISH"


def detect_three_white_soldiers(candles: pd.DataFrame) -> PatternResult:
    a, b, c = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
    detected = all(_is_green(x) for x in (a, b, c)) and float(a.close) < float(b.close) < float(c.close)
    return detected, 85 if _volume_confirmed(candles) else 75, "BULLISH"


def detect_three_black_crows(candles: pd.DataFrame) -> PatternResult:
    a, b, c = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
    detected = all(_is_red(x) for x in (a, b, c)) and float(a.close) > float(b.close) > float(c.close)
    return detected, 85 if _volume_confirmed(candles) else 75, "BEARISH"


def detect_three_inside_up(candles: pd.DataFrame) -> PatternResult:
    a, b, c = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
    harami = _is_red(a) and _is_green(b) and float(b.open) > float(a.close) and float(b.close) < float(a.open)
    detected = harami and _is_green(c) and float(c.close) > float(a.open)
    return detected, 75, "BULLISH"


def detect_three_inside_down(candles: pd.DataFrame) -> PatternResult:
    a, b, c = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
    harami = _is_green(a) and _is_red(b) and float(b.open) < float(a.close) and float(b.close) > float(a.open)
    detected = harami and _is_red(c) and float(c.close) < float(a.open)
    return detected, 75, "BEARISH"


PATTERNS: dict[str, tuple[PatternFn, str, int]] = {
    "Doji": (detect_doji, "SINGLE", 1),
    "Hammer": (detect_hammer, "SINGLE", 4),
    "Shooting Star": (detect_shooting_star, "SINGLE", 4),
    "Bullish Marubozu": (detect_marubozu_bull, "SINGLE", 1),
    "Bearish Marubozu": (detect_marubozu_bear, "SINGLE", 1),
    "Spinning Top": (detect_spinning_top, "SINGLE", 1),
    "Bullish Engulfing": (detect_bullish_engulfing, "TWO_CANDLE", 2),
    "Bearish Engulfing": (detect_bearish_engulfing, "TWO_CANDLE", 2),
    "Bullish Harami": (detect_bullish_harami, "TWO_CANDLE", 2),
    "Bearish Harami": (detect_bearish_harami, "TWO_CANDLE", 2),
    "Tweezer Bottom": (detect_tweezer_bottom, "TWO_CANDLE", 2),
    "Tweezer Top": (detect_tweezer_top, "TWO_CANDLE", 2),
    "Morning Star": (detect_morning_star, "THREE_CANDLE", 3),
    "Evening Star": (detect_evening_star, "THREE_CANDLE", 3),
    "Three White Soldiers": (detect_three_white_soldiers, "THREE_CANDLE", 3),
    "Three Black Crows": (detect_three_black_crows, "THREE_CANDLE", 3),
    "Three Inside Up": (detect_three_inside_up, "THREE_CANDLE", 3),
    "Three Inside Down": (detect_three_inside_down, "THREE_CANDLE", 3),
}


def _read_candles(conn: sqlite3.Connection, symbol: str, interval: str, limit: int = 20) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT timestamp, open, high, low, close, volume
        FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (symbol, interval, limit),
    ).fetchall()
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return frame.sort_values("timestamp").reset_index(drop=True)


def run_all_patterns(symbol: str, interval: str = "15m") -> list[dict[str, Any]]:
    normalized_symbol = symbol.upper().strip()
    with _get_connection() as conn:
        ensure_patterns_schema(conn)
        candles = _read_candles(conn, normalized_symbol, interval, 20)
        if len(candles) < 1:
            return []
        detected_patterns: list[dict[str, Any]] = []
        timestamp = str(candles.iloc[-1].timestamp)
        for pattern_name, (function, pattern_type, candles_used) in PATTERNS.items():
            if len(candles) < candles_used:
                continue
            detected, strength, direction = function(candles.tail(max(candles_used, 4)))
            if not detected:
                continue
            volume_confirmed = 1 if _volume_confirmed(candles) else 0
            item = {
                "symbol": normalized_symbol,
                "interval": interval,
                "timestamp": timestamp,
                "pattern_name": pattern_name,
                "pattern_type": pattern_type,
                "direction": direction,
                "strength": round(float(strength), 2),
                "volume_confirmed": volume_confirmed,
                "candles_used": candles_used,
            }
            conn.execute(
                """
                INSERT INTO patterns (
                    symbol, interval, timestamp, pattern_name, pattern_type,
                    direction, strength, volume_confirmed, candles_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, timestamp, pattern_name)
                DO UPDATE SET
                    pattern_type = excluded.pattern_type,
                    direction = excluded.direction,
                    strength = excluded.strength,
                    volume_confirmed = excluded.volume_confirmed,
                    candles_used = excluded.candles_used,
                    detected_at = datetime('now')
                """,
                (
                    item["symbol"],
                    item["interval"],
                    item["timestamp"],
                    item["pattern_name"],
                    item["pattern_type"],
                    item["direction"],
                    item["strength"],
                    item["volume_confirmed"],
                    item["candles_used"],
                ),
            )
            detected_patterns.append(item)
        conn.commit()
    return detected_patterns


def get_pattern_signal_modifier(symbol: str) -> dict[str, Any]:
    with _get_connection() as conn:
        ensure_patterns_schema(conn)
        latest_rows = conn.execute(
            """
            SELECT timestamp
            FROM candles
            WHERE UPPER(symbol) = UPPER(?)
            ORDER BY timestamp DESC
            LIMIT 3
            """,
            (symbol,),
        ).fetchall()
        latest_timestamps = [row["timestamp"] for row in latest_rows]
        if not latest_timestamps:
            return {"modifier": 0.0, "patterns": []}
        placeholders = ",".join("?" for _ in latest_timestamps)
        rows = conn.execute(
            f"""
            SELECT *
            FROM patterns
            WHERE UPPER(symbol) = UPPER(?)
              AND timestamp IN ({placeholders})
            ORDER BY timestamp DESC, strength DESC
            """,
            (symbol, *latest_timestamps),
        ).fetchall()
    modifier = 0.0
    patterns = rows_to_dicts(rows)
    for pattern in patterns:
        strength = float(pattern.get("strength") or 0)
        direction = pattern.get("direction")
        if direction == "BULLISH":
            modifier += 20 if strength > 70 else 10 if strength >= 50 else 0
        elif direction == "BEARISH":
            modifier -= 20 if strength > 70 else 10 if strength >= 50 else 0
    return {"modifier": max(-30.0, min(30.0, modifier)), "patterns": patterns}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_pattern_win_rates() -> dict[str, float]:
    with _get_connection() as conn:
        ensure_patterns_schema(conn)
        trade_columns = {row["name"] for row in conn.execute("PRAGMA table_info(paper_trades)")}
        if not {"symbol", "outcome"}.issubset(trade_columns):
            return {}
        time_column = "entry_time" if "entry_time" in trade_columns else "created_at"
        rows = conn.execute(
            f"""
            SELECT p.pattern_name,
                   COUNT(t.id) AS total_trades,
                   SUM(CASE WHEN t.outcome = 'WIN' THEN 1 ELSE 0 END) AS wins
            FROM patterns p
            JOIN paper_trades t
              ON UPPER(t.symbol) = UPPER(p.symbol)
             AND datetime(p.timestamp) <= datetime(t.{time_column})
             AND datetime(p.timestamp) >= datetime(t.{time_column}, '-1 day')
            WHERE t.outcome IS NOT NULL
            GROUP BY p.pattern_name
            """
        ).fetchall()
    return {
        row["pattern_name"]: round(float(row["wins"] or 0) / float(row["total_trades"] or 1), 4)
        for row in rows
    }


if __name__ == "__main__":
    print(run_all_patterns("RELIANCE"))
