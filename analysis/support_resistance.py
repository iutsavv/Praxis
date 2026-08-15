"""Support and resistance detection from stored candle data."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"


@dataclass
class Touch:
    timestamp: str
    price: float
    volume: float


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_price_levels_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_levels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            level_price     REAL NOT NULL,
            level_type      TEXT NOT NULL,
            touches         INTEGER DEFAULT 0,
            strength_score  REAL DEFAULT 0,
            avg_volume_at_touch REAL,
            first_touch     TEXT,
            last_touch      TEXT,
            broken_at       TEXT,
            is_active       INTEGER DEFAULT 1,
            last_updated    TEXT,
            UNIQUE(symbol, timeframe, level_price, level_type)
        )
        """
    )
    conn.commit()


def find_swing_highs(candles: pd.DataFrame, window: int = 5) -> list[tuple[str, float]]:
    """Return candles whose high is higher than N candles on both sides."""
    if candles.empty or len(candles) < (window * 2) + 1:
        return []
    highs = candles["high"].astype(float).tolist()
    timestamps = candles["timestamp"].astype(str).tolist()
    swings: list[tuple[str, float]] = []
    for idx in range(window, len(highs) - window):
        price = highs[idx]
        left = highs[idx - window : idx]
        right = highs[idx + 1 : idx + window + 1]
        if all(price > value for value in left) and all(price > value for value in right):
            swings.append((timestamps[idx], float(price)))
    return swings


def find_swing_lows(candles: pd.DataFrame, window: int = 5) -> list[tuple[str, float]]:
    """Return candles whose low is lower than N candles on both sides."""
    if candles.empty or len(candles) < (window * 2) + 1:
        return []
    lows = candles["low"].astype(float).tolist()
    timestamps = candles["timestamp"].astype(str).tolist()
    swings: list[tuple[str, float]] = []
    for idx in range(window, len(lows) - window):
        price = lows[idx]
        left = lows[idx - window : idx]
        right = lows[idx + 1 : idx + window + 1]
        if all(price < value for value in left) and all(price < value for value in right):
            swings.append((timestamps[idx], float(price)))
    return swings


def cluster_price_levels(prices: list[float], tolerance_pct: float = 0.003) -> list[tuple[float, int]]:
    """Group prices within tolerance into median-price clusters."""
    clean_prices = sorted(float(price) for price in prices if price and price > 0)
    if not clean_prices:
        return []

    clusters: list[list[float]] = []
    for price in clean_prices:
        if not clusters:
            clusters.append([price])
            continue
        center = median(clusters[-1])
        if abs(price - center) / center <= tolerance_pct:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    return [(round(float(median(cluster)), 2), len(cluster)) for cluster in clusters]


def calculate_level_strength(
    level_price: float,
    touches: int,
    volume_at_touches: list[float],
    recency_days: float,
) -> float:
    """Score level strength from touches, touch volume ratio, and recency."""
    base = min(75.0, max(0, touches) * 15.0)
    vol_ratio = sum(volume_at_touches) / len(volume_at_touches) if volume_at_touches else 1.0
    volume_bonus = min(25.0, vol_ratio * 10.0)
    recency_bonus = max(0.0, 15.0 - max(0.0, recency_days) * 0.5)
    return round(min(100.0, base + volume_bonus + recency_bonus), 2)


def _read_candles(conn: sqlite3.Connection, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT timestamp, open, high, low, close, volume
        FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (symbol, timeframe, limit),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame([dict(row) for row in rows])
    return frame.sort_values("timestamp").reset_index(drop=True)


def _read_daily_candles(conn: sqlite3.Connection, symbol: str, limit: int = 260) -> pd.DataFrame:
    daily = _read_candles(conn, symbol, "1d", limit)
    if not daily.empty:
        return daily
    rows = conn.execute(
        """
        SELECT substr(timestamp, 1, 10) AS day,
               MAX(high) AS high,
               MIN(low) AS low,
               MAX(close) AS close,
               SUM(volume) AS volume
        FROM candles
        WHERE UPPER(symbol) = UPPER(?)
        GROUP BY substr(timestamp, 1, 10)
        ORDER BY day DESC
        LIMIT ?
        """,
        (symbol, limit),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "high", "low", "close", "volume"])
    frame = pd.DataFrame(
        {
            "timestamp": row["day"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in rows
    )
    return frame.sort_values("timestamp").reset_index(drop=True)


def _parse_timestamp(value: str) -> datetime:
    text = str(value or "").replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now()


def _recency_days(timestamp: str) -> float:
    return max(0.0, (datetime.now() - _parse_timestamp(timestamp)).total_seconds() / 86400)


def _touches_for_level(candles: pd.DataFrame, level_price: float, tolerance_pct: float = 0.003) -> list[Touch]:
    touches: list[Touch] = []
    for row in candles.itertuples(index=False):
        high = float(row.high)
        low = float(row.low)
        if low <= level_price <= high or min(abs(high - level_price), abs(low - level_price)) / level_price <= tolerance_pct:
            touches.append(Touch(str(row.timestamp), level_price, float(row.volume or 0)))
    return touches


def _volume_ratios(touches: list[Touch], avg_volume: float) -> list[float]:
    if avg_volume <= 0:
        return [1.0 for _ in touches]
    return [max(0.1, touch.volume / avg_volume) for touch in touches]


def _round_numbers_near(current_price: float) -> list[float]:
    if current_price <= 0:
        return []
    steps = [10.0]
    if current_price >= 250:
        steps.append(50.0)
    if current_price >= 1000:
        steps.append(100.0)
    candidates: set[float] = set()
    for step in steps:
        center = round(current_price / step) * step
        candidates.update({center - step, center, center + step})
    return sorted(price for price in candidates if price > 0 and abs(price - current_price) / current_price <= 0.02)


def _build_level(
    candles: pd.DataFrame,
    level_price: float,
    touches: int,
    level_type: str,
    source: str,
) -> dict[str, Any]:
    touch_rows = _touches_for_level(candles, level_price)
    if not touch_rows and not candles.empty:
        latest = candles.iloc[-1]
        touch_rows = [Touch(str(latest["timestamp"]), level_price, float(latest["volume"] or 0))]
    avg_volume = float(candles["volume"].astype(float).mean()) if not candles.empty else 0.0
    first_touch = touch_rows[0].timestamp if touch_rows else None
    last_touch = touch_rows[-1].timestamp if touch_rows else None
    touch_count = max(touches, len(touch_rows))
    avg_volume_at_touch = sum(touch.volume for touch in touch_rows) / len(touch_rows) if touch_rows else None
    strength = calculate_level_strength(
        level_price,
        touch_count,
        _volume_ratios(touch_rows, avg_volume),
        _recency_days(last_touch) if last_touch else 999,
    )
    return {
        "level_price": round(float(level_price), 2),
        "level_type": level_type,
        "touches": int(touch_count),
        "strength_score": strength,
        "avg_volume_at_touch": round(avg_volume_at_touch, 2) if avg_volume_at_touch is not None else None,
        "first_touch": first_touch,
        "last_touch": last_touch,
        "source": source,
    }


def _dedupe_levels(levels: list[dict[str, Any]], tolerance_pct: float = 0.003) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for level in sorted(levels, key=lambda item: (-item["strength_score"], item["level_price"])):
        if any(abs(level["level_price"] - kept["level_price"]) / kept["level_price"] <= tolerance_pct for kept in deduped):
            continue
        deduped.append(level)
    return sorted(deduped, key=lambda item: item["level_price"])


def _upsert_levels(conn: sqlite3.Connection, symbol: str, timeframe: str, levels: list[dict[str, Any]]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for level in levels:
        conn.execute(
            """
            INSERT INTO price_levels (
                symbol, timeframe, level_price, level_type, touches,
                strength_score, avg_volume_at_touch, first_touch, last_touch,
                broken_at, is_active, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?)
            ON CONFLICT(symbol, timeframe, level_price, level_type)
            DO UPDATE SET
                touches = excluded.touches,
                strength_score = excluded.strength_score,
                avg_volume_at_touch = excluded.avg_volume_at_touch,
                first_touch = excluded.first_touch,
                last_touch = excluded.last_touch,
                broken_at = NULL,
                is_active = 1,
                last_updated = excluded.last_updated
            """,
            (
                symbol.upper(),
                timeframe,
                level["level_price"],
                level["level_type"],
                level["touches"],
                level["strength_score"],
                level["avg_volume_at_touch"],
                level["first_touch"],
                level["last_touch"],
                now,
            ),
        )


def find_all_levels(symbol: str, timeframe: str = "15m") -> dict[str, list[dict[str, Any]]]:
    """Find, score, store, and return support/resistance levels for a symbol."""
    normalized_symbol = symbol.upper().strip()
    with _get_connection() as conn:
        ensure_price_levels_schema(conn)
        candles = _read_candles(conn, normalized_symbol, timeframe, 200)
        if candles.empty:
            return {"support": [], "resistance": []}

        current_price = float(candles.iloc[-1]["close"])
        highs = find_swing_highs(candles)
        lows = find_swing_lows(candles)
        support: list[dict[str, Any]] = []
        resistance: list[dict[str, Any]] = []

        for price, count in cluster_price_levels([price for _, price in lows]):
            support.append(_build_level(candles, price, count, "SUPPORT", "swing_low"))
        for price, count in cluster_price_levels([price for _, price in highs]):
            resistance.append(_build_level(candles, price, count, "RESISTANCE", "swing_high"))

        daily = _read_daily_candles(conn, normalized_symbol)
        if len(daily) >= 2:
            previous_day = daily.iloc[-2]
            resistance.append(_build_level(candles, float(previous_day["high"]), 1, "RESISTANCE", "pdh"))
            support.append(_build_level(candles, float(previous_day["low"]), 1, "SUPPORT", "pdl"))
        if not daily.empty:
            resistance.append(_build_level(candles, float(daily["high"].astype(float).max()), 1, "RESISTANCE", "52w_high"))
            support.append(_build_level(candles, float(daily["low"].astype(float).min()), 1, "SUPPORT", "52w_low"))

        for round_price in _round_numbers_near(current_price):
            target = resistance if round_price >= current_price else support
            level_type = "RESISTANCE" if round_price >= current_price else "SUPPORT"
            target.append(_build_level(candles, round_price, 1, level_type, "round_number"))

        support = _dedupe_levels(support)
        resistance = _dedupe_levels(resistance)
        _upsert_levels(conn, normalized_symbol, timeframe, support + resistance)
        conn.commit()

    return {"support": support, "resistance": resistance}


def get_nearest_levels(symbol: str, current_price: float, n: int = 3) -> dict[str, list[dict[str, Any]]]:
    """Return nearest active supports below and resistances above current price."""
    with _get_connection() as conn:
        ensure_price_levels_schema(conn)
        rows = conn.execute(
            """
            SELECT level_price, level_type, touches, strength_score, last_touch
            FROM price_levels
            WHERE UPPER(symbol) = UPPER(?) AND is_active = 1
            """,
            (symbol,),
        ).fetchall()

    support = [
        dict(row) | {"distance_pct": round((current_price - float(row["level_price"])) / current_price * 100, 3)}
        for row in rows
        if row["level_type"] == "SUPPORT" and float(row["level_price"]) <= current_price
    ]
    resistance = [
        dict(row) | {"distance_pct": round((float(row["level_price"]) - current_price) / current_price * 100, 3)}
        for row in rows
        if row["level_type"] == "RESISTANCE" and float(row["level_price"]) >= current_price
    ]
    support.sort(key=lambda row: row["distance_pct"])
    resistance.sort(key=lambda row: row["distance_pct"])
    return {"support": support[:n], "resistance": resistance[:n]}


def get_level_signal_modifier(symbol: str, current_price: float) -> dict[str, Any]:
    """Return signal score adjustment based on nearest active price levels."""
    nearest = get_nearest_levels(symbol, current_price, 1)
    with _get_connection() as conn:
        ensure_price_levels_schema(conn)
        close_rows = conn.execute(
            """
            SELECT close FROM candles
            WHERE UPPER(symbol) = UPPER(?) AND interval = '15m'
            ORDER BY timestamp DESC
            LIMIT 2
            """,
            (symbol,),
        ).fetchall()
        previous_close = float(close_rows[1]["close"]) if len(close_rows) >= 2 else current_price
        breakout_row = conn.execute(
            """
            SELECT level_price FROM price_levels
            WHERE UPPER(symbol) = UPPER(?)
              AND is_active = 1
              AND level_type = 'RESISTANCE'
              AND level_price >= ?
              AND level_price < ?
            ORDER BY level_price DESC
            LIMIT 1
            """,
            (symbol, previous_close, current_price),
        ).fetchone()
        breakdown_row = conn.execute(
            """
            SELECT level_price FROM price_levels
            WHERE UPPER(symbol) = UPPER(?)
              AND is_active = 1
              AND level_type = 'SUPPORT'
              AND level_price <= ?
              AND level_price > ?
            ORDER BY level_price ASC
            LIMIT 1
            """,
            (symbol, previous_close, current_price),
        ).fetchone()
    support = nearest["support"][0] if nearest["support"] else None
    resistance = nearest["resistance"][0] if nearest["resistance"] else None
    support_price = float(support["level_price"]) if support else None
    resistance_price = float(resistance["level_price"]) if resistance else None
    distance_to_support = ((current_price - support_price) / current_price * 100) if support_price else None
    distance_to_resistance = ((resistance_price - current_price) / current_price * 100) if resistance_price else None
    at_support = bool(distance_to_support is not None and 0 <= distance_to_support <= 0.3)
    at_resistance = bool(distance_to_resistance is not None and 0 <= distance_to_resistance <= 0.3)
    breakout_price = float(breakout_row["level_price"]) if breakout_row else None
    breakdown_price = float(breakdown_row["level_price"]) if breakdown_row else None
    breaking_out = bool(breakout_price is not None)
    breaking_down = bool(breakdown_price is not None)

    modifier = 0.0
    reasons: list[str] = []
    if at_support:
        modifier += 20.0
        reasons.append("price is within 0.3% of support")
    if at_resistance:
        modifier -= 20.0
        reasons.append("price is within 0.3% of resistance")
    if resistance and distance_to_resistance is not None and 0.3 < distance_to_resistance <= 1.0:
        modifier -= 8.0
        reasons.append("resistance is nearby")
    if support and distance_to_support is not None and 0.3 < distance_to_support <= 1.0:
        modifier += 8.0
        reasons.append("support is nearby")
    if breaking_out:
        modifier += 30.0
        reasons.append("price closed above resistance")
    if breaking_down:
        modifier -= 30.0
        reasons.append("price closed below support")

    return {
        "modifier": round(max(-30.0, min(30.0, modifier)), 2),
        "reason": "; ".join(reasons) if reasons else "price is between nearby support and resistance",
        "nearest_support": support_price,
        "nearest_resistance": resistance_price,
        "distance_to_support_pct": round(distance_to_support, 3) if distance_to_support is not None else None,
        "distance_to_resistance_pct": round(distance_to_resistance, 3) if distance_to_resistance is not None else None,
        "at_support": at_support,
        "at_resistance": at_resistance,
        "breaking_out": breaking_out,
        "breaking_down": breaking_down,
    }


def _latest_close(conn: sqlite3.Connection, symbol: str, timeframe: str = "15m") -> float | None:
    row = conn.execute(
        """
        SELECT close FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, timeframe),
    ).fetchone()
    return float(row["close"]) if row else None


def _mark_broken_levels(conn: sqlite3.Connection, symbol: str, timeframe: str = "15m") -> int:
    levels = conn.execute(
        """
        SELECT id, level_price, level_type
        FROM price_levels
        WHERE UPPER(symbol) = UPPER(?) AND timeframe = ? AND is_active = 1
        """,
        (symbol, timeframe),
    ).fetchall()
    if not levels:
        return 0
    closes = conn.execute(
        """
        SELECT close FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 10
        """,
        (symbol, timeframe),
    ).fetchall()
    close_values = [float(row["close"]) for row in closes]
    broken = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for level in levels:
        price = float(level["level_price"])
        if level["level_type"] == "SUPPORT":
            break_count = sum(1 for close in close_values if close < price)
        else:
            break_count = sum(1 for close in close_values if close > price)
        if break_count >= 3:
            conn.execute(
                "UPDATE price_levels SET is_active = 0, broken_at = ?, last_updated = ? WHERE id = ?",
                (now, now, level["id"]),
            )
            broken += 1
    return broken


def update_all_levels() -> dict[str, Any]:
    """Refresh levels for all active stocks and deactivate repeatedly broken levels."""
    with _get_connection() as conn:
        ensure_price_levels_schema(conn)
        symbols = [
            row["symbol"]
            for row in conn.execute("SELECT symbol FROM stock_universe WHERE is_active = 1 ORDER BY symbol").fetchall()
        ]

    updated = 0
    skipped = 0
    levels_saved = 0
    broken = 0
    errors: list[str] = []
    for symbol in symbols:
        try:
            levels = find_all_levels(symbol, "15m")
            count = len(levels["support"]) + len(levels["resistance"])
            if count:
                updated += 1
                levels_saved += count
            else:
                skipped += 1
            with _get_connection() as conn:
                ensure_price_levels_schema(conn)
                broken += _mark_broken_levels(conn, symbol, "15m")
                conn.commit()
        except Exception as exc:
            skipped += 1
            errors.append(f"{symbol}: {exc}")

    return {
        "symbols_checked": len(symbols),
        "symbols_updated": updated,
        "symbols_skipped": skipped,
        "levels_saved": levels_saved,
        "levels_marked_inactive": broken,
        "errors": errors[:20],
    }


if __name__ == "__main__":
    print(update_all_levels())
