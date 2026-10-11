"""Setup matching engine for NSE paper-trading signals."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from analysis.support_resistance import get_nearest_levels
except ImportError:
    def get_nearest_levels(symbol: str, current_price: float, n: int = 3) -> dict[str, list[dict[str, Any]]]:
        return {"support": [], "resistance": []}

try:
    from analysis.pattern_detector import run_all_patterns
except ImportError:
    def run_all_patterns(symbol: str, interval: str = "15m") -> list[dict[str, Any]]:
        return []

try:
    from analysis.multi_timeframe import get_timeframe_confluence
except ImportError:
    def get_timeframe_confluence(symbol: str) -> dict[str, Any]:
        return {"1h": "NEUTRAL", "confluence_score": 0, "trade_direction": "NONE"}


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = timezone(timedelta(hours=5, minutes=30))
INTERVAL = "15m"
PARTIAL_MATCH_THRESHOLD = 60.0
log = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct_distance(a: float, b: float) -> float:
    return abs(a - b) / b * 100 if b else 999.0


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _current_time_text() -> str:
    return datetime.now(IST).strftime("%H:%M")


def _minutes_since_open(now: datetime | None = None) -> int:
    current = now or datetime.now(IST)
    market_open = datetime.combine(current.date(), time(9, 15), tzinfo=IST)
    return max(0, int((current - market_open).total_seconds() // 60))


def _latest_candles(conn: sqlite3.Connection, symbol: str, interval: str = INTERVAL, limit: int = 80) -> pd.DataFrame:
    if not _table_exists(conn, "candles"):
        return pd.DataFrame()
    return pd.read_sql_query(
        """
        SELECT *
        FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        conn,
        params=(symbol, interval, limit),
    ).iloc[::-1].reset_index(drop=True)


def _latest_indicator_rows(conn: sqlite3.Connection, symbol: str, limit: int = 8) -> list[sqlite3.Row]:
    if not _table_exists(conn, "indicators"):
        return []
    return conn.execute(
        """
        SELECT i.*, c.open, c.high, c.low, c.close, c.volume
        FROM indicators i
        LEFT JOIN candles c ON c.id = i.candle_id
        WHERE UPPER(i.symbol) = UPPER(?) AND i.interval = ?
        ORDER BY i.timestamp DESC
        LIMIT ?
        """,
        (symbol, INTERVAL, limit),
    ).fetchall()


def _recent_db_patterns(conn: sqlite3.Connection, symbol: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "patterns"):
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM patterns
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC, detected_at DESC
        LIMIT 20
        """,
        (symbol, INTERVAL),
    ).fetchall()
    return [dict(row) for row in rows]


def _pattern_list(symbol: str, db_patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {
        _normalize_name(row.get("pattern_name")): row for row in db_patterns
    }
    try:
        for pattern in run_all_patterns(symbol, interval=INTERVAL):
            by_key[_normalize_name(pattern.get("pattern_name") or pattern.get("name"))] = pattern
    except Exception as exc:
        log.debug("Pattern detector failed for %s: %s", symbol, exc)
    return list(by_key.values())


def _news_features(conn: sqlite3.Connection, symbol: str) -> tuple[float, float]:
    sentiment = 0.0
    boost = 0.0
    if _table_exists(conn, "news"):
        cols = _columns(conn, "news")
        if "sentiment_score" in cols:
            row = conn.execute(
                """
                SELECT AVG(sentiment_score) AS sentiment
                FROM news
                WHERE UPPER(symbol) = UPPER(?) OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?
                ORDER BY COALESCE(published_at, fetched_at) DESC
                LIMIT 5
                """,
                (symbol, f"%{symbol.upper()}%"),
            ).fetchone()
            sentiment = _safe_float(row["sentiment"] if row else 0.0)
    if _table_exists(conn, "announcements"):
        row = conn.execute(
            """
            SELECT MAX(COALESCE(signal_boost, impact_score, 0)) AS boost,
                   AVG(COALESCE(sentiment_score, 0)) AS sentiment
            FROM announcements
            WHERE UPPER(symbol) = UPPER(?)
            ORDER BY COALESCE(published_at, fetched_at) DESC
            LIMIT 5
            """,
            (symbol,),
        ).fetchone()
        if row:
            sentiment = max(sentiment, _safe_float(row["sentiment"]))
            boost = _safe_float(row["boost"])
    return sentiment, boost


def _latest_market_condition(conn: sqlite3.Connection, symbol: str) -> str:
    if not _table_exists(conn, "signal_scores"):
        return "SIDEWAYS"
    row = conn.execute(
        """
        SELECT market_condition
        FROM signal_scores
        WHERE UPPER(symbol) = UPPER(?)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return str(row["market_condition"] if row else "SIDEWAYS").upper()


def _trend(candles: pd.DataFrame, ema20: float, ema50: float) -> str:
    if ema20 and ema50 and ema20 > ema50:
        return "UP"
    if ema20 and ema50 and ema20 < ema50:
        return "DOWN"
    if len(candles) >= 20:
        first = _safe_float(candles.iloc[-20]["close"])
        last = _safe_float(candles.iloc[-1]["close"])
        if last > first * 1.005:
            return "UP"
        if last < first * 0.995:
            return "DOWN"
    return "SIDEWAYS"


def _structure_features(candles: pd.DataFrame) -> dict[str, Any]:
    if candles.empty:
        return {
            "higher_lows_count": 0,
            "lower_highs_count": 0,
            "consolidation_bars": 0,
            "opening_range_high": 0.0,
            "two_swing_lows_within_pct": 999.0,
            "two_swing_highs_within_pct": 999.0,
            "bars_between_lows": 0,
            "bars_between_highs": 0,
        }

    recent = candles.tail(20).copy()
    lows = recent["low"].astype(float).tolist()
    highs = recent["high"].astype(float).tolist()
    closes = recent["close"].astype(float)
    higher_lows = sum(1 for prev, cur in zip(lows, lows[1:]) if cur > prev)
    lower_highs = sum(1 for prev, cur in zip(highs, highs[1:]) if cur < prev)

    range_pct = (recent["high"].max() - recent["low"].min()) / closes.iloc[-1] * 100 if closes.iloc[-1] else 999.0
    consolidation_bars = len(recent) if range_pct <= 2.0 else max(0, int(len(recent) * max(0, 2.0 / range_pct)))

    low_series = recent["low"].astype(float).reset_index(drop=True)
    high_series = recent["high"].astype(float).reset_index(drop=True)
    low_positions = low_series.nsmallest(min(2, len(low_series))).index.tolist()
    high_positions = high_series.nlargest(min(2, len(high_series))).index.tolist()
    two_lows_pct = _pct_distance(low_series.iloc[low_positions[0]], low_series.iloc[low_positions[1]]) if len(low_positions) == 2 else 999.0
    two_highs_pct = _pct_distance(high_series.iloc[high_positions[0]], high_series.iloc[high_positions[1]]) if len(high_positions) == 2 else 999.0

    return {
        "higher_lows_count": higher_lows,
        "lower_highs_count": lower_highs,
        "consolidation_bars": consolidation_bars,
        "opening_range_high": _safe_float(candles.head(2)["high"].max()),
        "two_swing_lows_within_pct": two_lows_pct,
        "two_swing_highs_within_pct": two_highs_pct,
        "bars_between_lows": abs(low_positions[0] - low_positions[1]) if len(low_positions) == 2 else 0,
        "bars_between_highs": abs(high_positions[0] - high_positions[1]) if len(high_positions) == 2 else 0,
    }


def _gather_stock_data(symbol: str) -> dict[str, Any]:
    """Gather the market snapshot needed by setup conditions."""
    normalized_symbol = symbol.upper().strip()
    data: dict[str, Any] = {
        "symbol": normalized_symbol,
        "patterns": [],
        "levels": {"support": [], "resistance": []},
        "confluence": {},
        "current_time": _current_time_text(),
        "time_since_open_minutes": _minutes_since_open(),
    }

    with _connect() as conn:
        indicators = _latest_indicator_rows(conn, normalized_symbol)
        candles = _latest_candles(conn, normalized_symbol)
        latest = indicators[0] if indicators else None

        if latest:
            for key in (
                "rsi", "macd", "macd_signal", "macd_histogram", "ema_20",
                "ema_50", "vwap", "volume_ratio", "open", "high", "low",
                "close", "volume",
            ):
                data[key] = latest[key]
        elif not candles.empty:
            last = candles.iloc[-1]
            for key in ("open", "high", "low", "close", "volume"):
                data[key] = last.get(key)

        data["macd_history"] = [_safe_float(row["macd"]) for row in indicators if row["macd"] is not None]
        data["signal_history"] = [_safe_float(row["macd_signal"]) for row in indicators if row["macd_signal"] is not None]
        data["candles"] = candles

        close = _safe_float(data.get("close"))
        try:
            data["levels"] = get_nearest_levels(normalized_symbol, close, 3)
        except Exception as exc:
            log.debug("Level lookup failed for %s: %s", normalized_symbol, exc)

        data["patterns"] = _pattern_list(normalized_symbol, _recent_db_patterns(conn, normalized_symbol))
        data["news_sentiment"], data["announcement_boost"] = _news_features(conn, normalized_symbol)
        data["market_condition"] = _latest_market_condition(conn, normalized_symbol)

    try:
        data["confluence"] = get_timeframe_confluence(normalized_symbol)
    except Exception as exc:
        log.debug("Timeframe confluence failed for %s: %s", normalized_symbol, exc)
        data["confluence"] = {}

    ema20 = _safe_float(data.get("ema_20"))
    ema50 = _safe_float(data.get("ema_50"))
    data["prior_trend"] = _trend(data["candles"], ema20, ema50)
    data.update(_structure_features(data["candles"]))
    return data


def _nearest(data: dict[str, Any], level_type: str) -> dict[str, Any]:
    levels = data.get("levels", {}).get(level_type, [])
    return levels[0] if levels else {}


def _macd_crossed(data: dict[str, Any], bars: int, above: bool) -> bool:
    macd = data.get("macd_history", [])
    signal = data.get("signal_history", [])
    lookback = min(max(1, bars), len(macd) - 1, len(signal) - 1)
    for index in range(lookback):
        current_delta = macd[index] - signal[index]
        previous_delta = macd[index + 1] - signal[index + 1]
        if above and current_delta > 0 >= previous_delta:
            return True
        if not above and current_delta < 0 <= previous_delta:
            return True
    return False


def _has_rsi_divergence(candles: pd.DataFrame | None, bullish: bool) -> bool:
    if candles is None or len(candles) < 8:
        return False
    closes = candles["close"].astype(float).tail(8).tolist()
    if bullish:
        return closes[-1] < min(closes[:-1]) * 1.002
    return closes[-1] > max(closes[:-1]) * 0.998


def _condition_met(key: str, expected: Any, data: dict[str, Any]) -> bool:
    close = _safe_float(data.get("close"))
    support = _nearest(data, "support")
    resistance = _nearest(data, "resistance")

    if key == "rsi_max":
        return _safe_float(data.get("rsi"), 999.0) <= _safe_float(expected)
    if key == "rsi_min":
        return _safe_float(data.get("rsi"), -999.0) >= _safe_float(expected)
    if key == "volume_ratio_min":
        return _safe_float(data.get("volume_ratio")) >= _safe_float(expected)
    if key == "distance_to_support_pct_max":
        return _safe_float(support.get("distance_pct"), 999.0) <= _safe_float(expected)
    if key == "distance_to_resistance_pct_max":
        return _safe_float(resistance.get("distance_pct"), 999.0) <= _safe_float(expected)
    if key == "candlestick_pattern":
        found = {_normalize_name(p.get("pattern_name") or p.get("name")) for p in data.get("patterns", [])}
        wanted = expected if isinstance(expected, list) else [expected]
        return any(_normalize_name(item) in found for item in wanted)
    if key == "timeframe_1h_bias":
        confluence = data.get("confluence", {})
        return str(confluence.get("1h") or confluence.get("bias_1h") or "").upper() == str(expected).upper()
    if key == "macd_crossed_above_signal_bars":
        return _macd_crossed(data, int(expected), above=True)
    if key == "macd_crossed_below_signal_bars":
        return _macd_crossed(data, int(expected), above=False)
    if key == "ema20_above_ema50":
        return (_safe_float(data.get("ema_20")) > _safe_float(data.get("ema_50"))) is bool(expected)
    if key == "ema20_below_ema50":
        return (_safe_float(data.get("ema_20")) < _safe_float(data.get("ema_50"))) is bool(expected)
    if key == "macd_histogram_positive":
        return (_safe_float(data.get("macd_histogram")) > 0) is bool(expected)
    if key == "macd_histogram_negative":
        return (_safe_float(data.get("macd_histogram")) < 0) is bool(expected)
    if key in ("price_above_resistance", "breakout_above_resistance"):
        return bool(expected) and bool(resistance) and close > _safe_float(resistance.get("level_price"))
    if key in ("price_below_support", "breakdown_below_support"):
        return bool(expected) and bool(support) and close < _safe_float(support.get("level_price"))
    if key == "price_above_vwap":
        return (close > _safe_float(data.get("vwap"))) is bool(expected)
    if key == "price_below_vwap":
        return (close < _safe_float(data.get("vwap"))) is bool(expected)
    if key == "price_near_vwap_pct":
        return _pct_distance(close, _safe_float(data.get("vwap"))) <= _safe_float(expected)
    if key == "price_near_ema20_pct":
        return _pct_distance(close, _safe_float(data.get("ema_20"))) <= _safe_float(expected)
    if key == "price_near_ema50_pct":
        return _pct_distance(close, _safe_float(data.get("ema_50"))) <= _safe_float(expected)
    if key == "price_above_ema20":
        return (close > _safe_float(data.get("ema_20"))) is bool(expected)
    if key == "price_above_ema50":
        return (close > _safe_float(data.get("ema_50"))) is bool(expected)
    if key == "prior_trend":
        return str(data.get("prior_trend", "")).upper() == str(expected).upper()
    if key == "volume_confirmed":
        return (_safe_float(data.get("volume_ratio")) >= 1.0) is bool(expected)
    if key == "news_sentiment_score_min":
        return _safe_float(data.get("news_sentiment")) >= _safe_float(expected)
    if key == "announcement_boost_min":
        return _safe_float(data.get("announcement_boost")) >= _safe_float(expected)
    if key == "time_since_open_minutes_max":
        return _safe_float(data.get("time_since_open_minutes")) <= _safe_float(expected)
    if key == "price_above_opening_range_high":
        return bool(expected) and close > _safe_float(data.get("opening_range_high"))
    if key == "higher_lows_count_min":
        return _safe_float(data.get("higher_lows_count")) >= _safe_float(expected)
    if key == "lower_highs_count_min":
        return _safe_float(data.get("lower_highs_count")) >= _safe_float(expected)
    if key == "flat_resistance_touches_min":
        return _safe_float(resistance.get("touches")) >= _safe_float(expected)
    if key == "flat_support_touches_min":
        return _safe_float(support.get("touches")) >= _safe_float(expected)
    if key == "two_swing_lows_within_pct_max":
        return _safe_float(data.get("two_swing_lows_within_pct"), 999.0) <= _safe_float(expected)
    if key == "two_swing_highs_within_pct_max":
        return _safe_float(data.get("two_swing_highs_within_pct"), 999.0) <= _safe_float(expected)
    if key == "bars_between_lows_min":
        return _safe_float(data.get("bars_between_lows")) >= _safe_float(expected)
    if key == "bars_between_lows_max":
        return _safe_float(data.get("bars_between_lows")) <= _safe_float(expected)
    if key == "bars_between_highs_min":
        return _safe_float(data.get("bars_between_highs")) >= _safe_float(expected)
    if key == "bars_between_highs_max":
        return _safe_float(data.get("bars_between_highs")) <= _safe_float(expected)
    if key == "volume_ratio_at_second_low_min":
        return _safe_float(data.get("volume_ratio")) >= _safe_float(expected)
    if key == "volume_ratio_at_second_high_min":
        return _safe_float(data.get("volume_ratio")) >= _safe_float(expected)
    if key == "consolidation_bars_min":
        return _safe_float(data.get("consolidation_bars")) >= _safe_float(expected)
    if key == "consolidation_bars_max":
        return _safe_float(data.get("consolidation_bars")) <= _safe_float(expected)
    if key in ("flag_slope_negative", "breakout_above_flag"):
        return bool(expected) and data.get("prior_trend") == "UP" and _safe_float(data.get("higher_lows_count")) >= 3
    if key in ("flag_slope_positive", "breakdown_below_flag"):
        return bool(expected) and data.get("prior_trend") == "DOWN" and _safe_float(data.get("lower_highs_count")) >= 3
    if key == "bullish_divergence":
        return bool(expected) and _has_rsi_divergence(data.get("candles"), bullish=True)
    if key == "bearish_divergence":
        return bool(expected) and _has_rsi_divergence(data.get("candles"), bullish=False)
    if key.endswith("_min"):
        return _safe_float(data.get(key[:-4])) >= _safe_float(expected)
    if key.endswith("_max"):
        return _safe_float(data.get(key[:-4]), 999.0) <= _safe_float(expected)
    return data.get(key) == expected


def check_setup_conditions(symbol: str, setup: dict, data: dict) -> dict:
    """Check each condition in a setup's JSON definition against live data."""
    try:
        conditions = json.loads(setup.get("conditions_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        conditions = {}

    if not conditions:
        return {
            "setup_code": setup.get("setup_code"),
            "matched": False,
            "conditions_met": [],
            "conditions_failed": ["conditions_json"],
            "match_quality": 0.0,
            "partial_match": False,
        }

    conditions_met: list[str] = []
    conditions_failed: list[str] = []
    for key, expected in conditions.items():
        try:
            if _condition_met(key, expected, data):
                conditions_met.append(key)
            else:
                conditions_failed.append(key)
        except Exception as exc:
            log.debug("Condition %s failed for %s: %s", key, symbol, exc)
            conditions_failed.append(key)

    match_quality = round(len(conditions_met) / len(conditions) * 100, 2)
    return {
        "setup_code": setup.get("setup_code"),
        "matched": len(conditions_failed) == 0,
        "conditions_met": conditions_met,
        "conditions_failed": conditions_failed,
        "match_quality": match_quality,
        "partial_match": 0 < len(conditions_failed) and match_quality > PARTIAL_MATCH_THRESHOLD,
    }


def _active_setups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    setups: list[dict[str, Any]] = []
    if _table_exists(conn, "setups"):
        rows = conn.execute(
            """
            SELECT *
            FROM setups
            WHERE COALESCE(is_active, 1) = 1
            ORDER BY COALESCE(win_rate, 0) DESC, setup_code ASC
            """
        ).fetchall()
        setups = [dict(row) for row in rows]
    if setups:
        return setups
    try:
        from brain.setup_library import SETUPS

        return [dict(setup) | {"win_rate": setup.get("win_rate", 0.0)} for setup in SETUPS]
    except Exception:
        return []


def match_all_setups(symbol: str) -> list[dict[str, Any]]:
    """Match every active setup for one symbol, including strong partials."""
    data = _gather_stock_data(symbol)
    with _connect() as conn:
        setups = _active_setups(conn)

    results: list[dict[str, Any]] = []
    for setup in setups:
        match = check_setup_conditions(symbol, setup, data)
        if match["matched"] or match["partial_match"]:
            results.append({**match, "setup_info": setup, "data": data})

    results.sort(key=lambda row: (row["matched"], row["match_quality"]), reverse=True)
    return results


def _market_condition_bonus(setup: dict[str, Any], data: dict[str, Any]) -> float:
    current = str(data.get("market_condition") or "").upper()
    best = str(setup.get("best_market_condition") or "").upper()
    avoid = str(setup.get("avoid_market_condition") or "").upper()
    if current and current == avoid:
        return 0.0
    if current and current == best:
        return 100.0
    return 50.0


def get_best_setup(symbol: str) -> dict[str, Any] | None:
    """Return the best fully matched setup for a symbol."""
    full_matches = [match for match in match_all_setups(symbol) if match["matched"]]
    if not full_matches:
        return None

    best: dict[str, Any] | None = None
    best_score = -1.0
    for match in full_matches:
        setup = match["setup_info"]
        score = (
            _safe_float(setup.get("win_rate")) * 0.5
            + _safe_float(match.get("match_quality")) * 0.3
            + _market_condition_bonus(setup, match["data"]) * 0.2
        )
        if score > best_score:
            best_score = score
            best = {**match, "composite_score": round(score, 2)}
    return best


def calculate_entry_price(setup: dict, candles: pd.DataFrame) -> float:
    """Calculate entry price from the setup entry method."""
    if candles.empty:
        return 0.0
    latest = candles.iloc[-1]
    method = setup.get("entry_method", "close_of_signal_candle")
    symbol = str(latest.get("symbol", setup.get("symbol", "")))
    close = _safe_float(latest.get("close"))

    if method in ("close_of_signal_candle", "close_of_crossover_candle", "next_candle_open"):
        return close
    if method == "breakout_price":
        levels = get_nearest_levels(symbol, close, 1).get("resistance", []) if symbol else []
        return _safe_float(levels[0].get("level_price"), close) if levels else close
    if method == "breakdown_price":
        levels = get_nearest_levels(symbol, close, 1).get("support", []) if symbol else []
        return _safe_float(levels[0].get("level_price"), close) if levels else close
    return close


def calculate_target_price(setup: dict, symbol: str, entry: float) -> float:
    """Calculate target price from the setup target method."""
    method = setup.get("target_method", "fixed_pct_2")
    direction = str(setup.get("direction_bias", "BUY")).upper()
    sign = 1 if direction == "BUY" else -1

    try:
        if method == "nearest_resistance":
            levels = get_nearest_levels(symbol, entry, 1).get("resistance", [])
            if levels:
                return _safe_float(levels[0].get("level_price"))
        if method == "nearest_support":
            levels = get_nearest_levels(symbol, entry, 1).get("support", [])
            if levels:
                return _safe_float(levels[0].get("level_price"))
        if method == "previous_swing_high":
            with _connect() as conn:
                row = conn.execute(
                    """
                    SELECT MAX(high) AS value
                    FROM (
                        SELECT high FROM candles
                        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
                        ORDER BY timestamp DESC
                        LIMIT 50
                    )
                    """,
                    (symbol, INTERVAL),
                ).fetchone()
                if row and row["value"]:
                    return _safe_float(row["value"])
        if method == "previous_swing_low":
            with _connect() as conn:
                row = conn.execute(
                    """
                    SELECT MIN(low) AS value
                    FROM (
                        SELECT low FROM candles
                        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
                        ORDER BY timestamp DESC
                        LIMIT 50
                    )
                    """,
                    (symbol, INTERVAL),
                ).fetchone()
                if row and row["value"]:
                    return _safe_float(row["value"])
        if method in ("measured_move", "neckline_projection"):
            return entry * (1 + sign * 0.02)
        if method == "fixed_pct_1_5":
            return entry * (1 + sign * 0.015)
        if method == "fixed_pct_2":
            return entry * (1 + sign * 0.02)
    except Exception as exc:
        log.debug("Target calculation failed for %s: %s", symbol, exc)
    return entry * (1 + sign * 0.02)


def calculate_stoploss_price(setup: dict, symbol: str, entry: float) -> float:
    """Calculate stop-loss price from the setup stop-loss method."""
    method = setup.get("stoploss_method", "fixed_pct_0_75")
    direction = str(setup.get("direction_bias", "BUY")).upper()
    is_buy = direction == "BUY"
    data = _gather_stock_data(symbol)

    try:
        if method == "below_support":
            levels = get_nearest_levels(symbol, entry, 1).get("support", [])
            if levels:
                return _safe_float(levels[0].get("level_price")) * 0.998
        if method == "above_resistance":
            levels = get_nearest_levels(symbol, entry, 1).get("resistance", [])
            if levels:
                return _safe_float(levels[0].get("level_price")) * 1.002
        if method == "below_ema20":
            return _safe_float(data.get("ema_20"), entry) * 0.998
        if method == "above_ema20":
            return _safe_float(data.get("ema_20"), entry) * 1.002
        if method == "below_ema50":
            return _safe_float(data.get("ema_50"), entry) * 0.998
        if method == "above_ema50":
            return _safe_float(data.get("ema_50"), entry) * 1.002
        if method == "below_signal_candle":
            return _safe_float(data.get("low"), entry) * 0.998
        if method == "above_signal_candle":
            return _safe_float(data.get("high"), entry) * 1.002
        if method == "fixed_pct_0_75":
            return entry * (0.9925 if is_buy else 1.0075)
        if method in ("below_breakout_level", "below_flag_low", "below_opening_range_low", "below_double_bottom", "below_last_higher_low", "below_vwap"):
            return min(entry, _safe_float(data.get("low"), entry), _safe_float(data.get("vwap"), entry)) * 0.998
        if method in ("above_breakdown_level", "above_flag_high", "above_double_top", "above_last_lower_high", "above_vwap"):
            return max(entry, _safe_float(data.get("high"), entry), _safe_float(data.get("vwap"), entry)) * 1.002
    except Exception as exc:
        log.debug("Stop-loss calculation failed for %s: %s", symbol, exc)
    return entry * (0.9925 if is_buy else 1.0075)


def _stage2_candidates() -> list[str]:
    with _connect() as conn:
        if _table_exists(conn, "scan_results"):
            row = conn.execute(
                "SELECT MAX(scan_time) AS scan_time FROM scan_results WHERE stage2_score IS NOT NULL"
            ).fetchone()
            if row and row["scan_time"]:
                rows = conn.execute(
                    """
                    SELECT DISTINCT symbol
                    FROM scan_results
                    WHERE scan_time = ?
                      AND stage2_score IS NOT NULL
                      AND COALESCE(stage2_direction, 'HOLD') != 'HOLD'
                    ORDER BY COALESCE(final_selected, 0) DESC, COALESCE(stage2_score, 0) DESC
                    """,
                    (row["scan_time"],),
                ).fetchall()
                symbols = [str(item["symbol"]).upper() for item in rows]
                if symbols:
                    return symbols
        if _table_exists(conn, "stock_universe") and "in_stage2_scan" in _columns(conn, "stock_universe"):
            rows = conn.execute(
                """
                SELECT symbol
                FROM stock_universe
                WHERE COALESCE(is_active, 1) = 1 AND COALESCE(in_stage2_scan, 0) = 1
                LIMIT 100
                """
            ).fetchall()
            symbols = [str(item["symbol"]).upper() for item in rows]
            if symbols:
                return symbols
        if _table_exists(conn, "stocks"):
            rows = conn.execute(
                "SELECT symbol FROM stocks WHERE COALESCE(is_active, 1) = 1 ORDER BY symbol"
            ).fetchall()
            return [str(item["symbol"]).upper() for item in rows]
    return []


def get_setup_match_for_all_stocks() -> list[dict[str, Any]]:
    """Return ranked setup plans for all latest Stage 2 candidates."""
    ranked: list[dict[str, Any]] = []
    for symbol in _stage2_candidates():
        match = get_best_setup(symbol)
        if not match:
            continue
        setup = {**match["setup_info"], "symbol": symbol}
        candles = match["data"].get("candles")
        if not isinstance(candles, pd.DataFrame) or candles.empty:
            candles = pd.DataFrame([{
                "symbol": symbol,
                "close": match["data"].get("close", 0.0),
                "high": match["data"].get("high", 0.0),
                "low": match["data"].get("low", 0.0),
            }])

        entry = calculate_entry_price(setup, candles)
        target = calculate_target_price(setup, symbol, entry)
        stop = calculate_stoploss_price(setup, symbol, entry)
        ranked.append({
            "symbol": symbol,
            "best_setup": setup,
            "setup_code": setup.get("setup_code"),
            "setup_name": setup.get("setup_name"),
            "direction_bias": setup.get("direction_bias"),
            "match_quality": match["match_quality"],
            "composite_score": match.get("composite_score", 0.0),
            "entry": round(entry, 2),
            "target": round(target, 2),
            "stop": round(stop, 2),
            "conditions_met": match["conditions_met"],
            "conditions_failed": match["conditions_failed"],
        })

    ranked.sort(key=lambda row: (row["composite_score"], row["match_quality"]), reverse=True)
    return ranked
