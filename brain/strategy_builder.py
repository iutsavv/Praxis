"""Autonomous trade-plan builder for Stage 2 NSE candidates.

Combines setup matching, local ML prediction, news sentiment, price levels,
and timeframe confluence into complete executable paper-trade plans.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from brain import ml_model, setup_matcher

try:
    from analysis.multi_timeframe import get_timeframe_confluence
except ImportError:
    def get_timeframe_confluence(symbol: str) -> dict[str, Any]:
        return {"confluence_level": "LOW", "trade_direction": "SKIP", "reasoning": "confluence unavailable"}

try:
    from analysis.pattern_detector import run_all_patterns
except ImportError:
    def run_all_patterns(symbol: str, interval: str = "15m") -> list[dict[str, Any]]:
        return []

try:
    from analysis.sentiment_engine import get_stock_news_sentiment
except ImportError:
    def get_stock_news_sentiment(symbol: str, hours: int = 24) -> dict[str, Any]:
        return {"symbol": symbol.upper(), "overall_sentiment": 0.0, "article_count": 0, "announcement_count": 0}

try:
    from analysis.support_resistance import get_nearest_levels
except ImportError:
    def get_nearest_levels(symbol: str, current_price: float, n: int = 3) -> dict[str, list[dict[str, Any]]]:
        return {"support": [], "resistance": []}


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
INTERVAL = "15m"
MIN_RISK_REWARD = 1.5
TRADEABLE_CONFIDENCE = 0.50
DEFAULT_SETUP_WIN_RATE = 0.55
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _normalize_ratio(value: Any, default: float = 0.0) -> float:
    number = _safe_float(value, default)
    if number > 1:
        number /= 100
    return max(0.0, min(1.0, number))


def _setup_win_rate(setup: dict[str, Any]) -> float:
    win_rate = _safe_float(setup.get("win_rate"), 0.0)
    if win_rate <= 0:
        return DEFAULT_SETUP_WIN_RATE
    return _normalize_ratio(win_rate)


def _round_price(value: float) -> float:
    return round(float(value), 2)


def _latest_candles(symbol: str, interval: str = INTERVAL, limit: int = 80) -> pd.DataFrame:
    with _connect() as conn:
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


def get_latest_indicators(symbol: str) -> dict[str, Any]:
    """Return the latest 15m indicator row for a symbol."""
    with _connect() as conn:
        if not _table_exists(conn, "indicators"):
            return {}
        row = conn.execute(
            """
            SELECT i.*, c.open, c.high, c.low, c.close, c.volume
            FROM indicators i
            LEFT JOIN candles c ON c.id = i.candle_id
            WHERE UPPER(i.symbol) = UPPER(?) AND i.interval = ?
            ORDER BY i.timestamp DESC
            LIMIT 1
            """,
            (symbol, INTERVAL),
        ).fetchone()
    return dict(row) if row else {}


def get_latest_patterns(symbol: str) -> list[dict[str, Any]]:
    """Return currently detected chart/candlestick patterns."""
    try:
        return run_all_patterns(symbol, interval=INTERVAL)
    except Exception as exc:
        log.debug("Pattern lookup failed for %s: %s", symbol, exc)
        return []


def _stage2_candidates() -> list[str]:
    """Read latest Stage 2 selected candidates from scan_results."""
    with _connect() as conn:
        if _table_exists(conn, "scan_results"):
            row = conn.execute(
                """
                SELECT MAX(scan_time) AS scan_time
                FROM scan_results
                WHERE stage2_score IS NOT NULL
                """
            ).fetchone()
            if row and row["scan_time"]:
                rows = conn.execute(
                    """
                    SELECT DISTINCT symbol
                    FROM scan_results
                    WHERE scan_time = ?
                      AND stage2_score IS NOT NULL
                      AND COALESCE(stage2_direction, 'HOLD') != 'HOLD'
                      AND COALESCE(final_selected, 0) = 1
                    ORDER BY COALESCE(stage2_score, 0) DESC
                    """,
                    (row["scan_time"],),
                ).fetchall()
                return [str(item["symbol"]).upper() for item in rows]
    return []


def _level_side(direction: str, target: bool) -> str:
    if direction.upper() == "BUY":
        return "resistance" if target else "support"
    return "support" if target else "resistance"


def _nearest_level(levels: dict[str, Any], side: str) -> dict[str, Any]:
    items = levels.get(side) if isinstance(levels, dict) else []
    return items[0] if items else {}


def _level_text(level: dict[str, Any], fallback: str) -> str:
    if not level:
        return fallback
    touches = int(_safe_float(level.get("touches") or level.get("touch_count"), 0))
    price = _safe_float(level.get("level_price") or level.get("price"), 0.0)
    if price and touches:
        return f"{fallback}, {touches} touches"
    if price:
        return fallback
    return fallback


def _format_money(value: Any) -> str:
    return f"₹{_safe_float(value):,.2f}"


def _indicator_line(indicators: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    rsi = indicators.get("rsi")
    if rsi is not None:
        rsi_value = _safe_float(rsi)
        if rsi_value <= 35:
            lines.append(f"✓ RSI oversold at {rsi_value:.0f} — historically bullish for this stock")
        elif rsi_value >= 65:
            lines.append(f"✓ RSI elevated at {rsi_value:.0f} — watch for exhaustion or continuation")
        else:
            lines.append(f"✓ RSI balanced at {rsi_value:.0f}")

    macd_hist = indicators.get("macd_histogram")
    if macd_hist is not None:
        macd_value = _safe_float(macd_hist)
        turn = "positive" if macd_value >= 0 else "negative"
        lines.append(f"✓ MACD histogram {turn} ({macd_value:.2f})")

    volume_ratio = indicators.get("volume_ratio")
    if volume_ratio is not None:
        volume_value = _safe_float(volume_ratio)
        if volume_value >= 1:
            lines.append(f"✓ Volume {volume_value:.1f}x average — confirming participation")
        else:
            lines.append(f"✓ Volume {volume_value:.1f}x average — lighter participation")
    return lines


def _confluence_text(confluence: dict[str, Any]) -> str:
    states = [str(confluence.get(tf, "NEUTRAL")).upper() for tf in ("5m", "15m", "1h")]
    bullish = states.count("BULLISH")
    bearish = states.count("BEARISH")
    if bullish == 3:
        return "✓ All 3 timeframes bullish (5m + 15m + 1h)"
    if bearish == 3:
        return "✓ All 3 timeframes bearish (5m + 15m + 1h)"
    reasoning = confluence.get("reasoning") or f"{confluence.get('confluence_level', 'LOW')} confluence"
    return f"✓ Timeframe confluence: {reasoning}"


def _news_text(news: dict[str, Any]) -> str:
    sentiment = _safe_float(news.get("overall_sentiment"))
    if sentiment > 0.2:
        label = "positive"
    elif sentiment < -0.2:
        label = "negative"
    else:
        label = "neutral"
    return f"✓ News {label} — sentiment score {sentiment:+.2f}"


def build_reasoning_string(
    symbol: str,
    best_setup: dict[str, Any],
    indicators: dict[str, Any],
    patterns: list[dict[str, Any]],
    levels: dict[str, Any],
    news: dict[str, Any],
    confluence: dict[str, Any],
    ml_prediction: dict[str, Any],
    entry: float | None = None,
    target: float | None = None,
    stop: float | None = None,
    rr_ratio: float | None = None,
) -> str:
    """Build a concise, human-readable explanation for a trade plan."""
    setup = best_setup.get("setup_info") if isinstance(best_setup.get("setup_info"), dict) else best_setup.get("setup", {})
    setup_name = setup.get("setup_name") or best_setup.get("setup_name") or "Matched Setup"
    direction = str(setup.get("direction_bias") or best_setup.get("direction_bias") or "BUY").upper()
    lines = [f"{direction} {symbol.upper()} — Setup: {setup_name}", ""]

    pattern_names = [str(p.get("pattern_name") or p.get("name")) for p in patterns if p.get("pattern_name") or p.get("name")]
    if pattern_names:
        lines.append(f"✓ {pattern_names[0]} pattern detected in the current setup area")

    conditions_met = best_setup.get("conditions_met") or []
    if conditions_met:
        lines.append(f"✓ Setup conditions matched: {', '.join(str(item) for item in conditions_met[:4])}")

    target_side = _level_side(direction, target=True)
    stop_side = _level_side(direction, target=False)
    target_level = _nearest_level(levels, target_side)
    stop_level = _nearest_level(levels, stop_side)
    if stop_level:
        lines.append(
            f"✓ Nearby {stop_side} at {_format_money(stop_level.get('level_price'))}"
            f" ({int(_safe_float(stop_level.get('touches'), 0))} touches)"
        )

    lines.extend(_indicator_line(indicators))
    lines.append(_confluence_text(confluence))
    lines.append(_news_text(news))
    lines.append(f"✓ ML model: {_safe_float(ml_prediction.get('win_probability')):.0%} win probability")

    if entry is not None and target is not None and stop is not None and rr_ratio is not None:
        lines.extend([
            "",
            f"Entry:  {_format_money(entry)}",
            f"Target: {_format_money(target)} ({_level_text(target_level, target_side)})",
            f"Stop:   {_format_money(stop)} (near {stop_side} / setup invalidation)",
            f"R/R:    {rr_ratio:.1f}:1",
        ])

    return "\n".join(lines)


def build_trade_plan(symbol: str) -> dict[str, Any]:
    """Build a complete autonomous trade plan for one Stage 2 symbol."""
    normalized_symbol = symbol.upper().strip()
    indicators = get_latest_indicators(normalized_symbol)
    candles = _latest_candles(normalized_symbol)
    current_price = _safe_float(indicators.get("close"))
    if current_price <= 0 and not candles.empty:
        current_price = _safe_float(candles.iloc[-1].get("close"))
    if current_price <= 0:
        return {"tradeable": False, "symbol": normalized_symbol, "reason": "No current market data"}

    patterns = get_latest_patterns(normalized_symbol)
    levels = get_nearest_levels(normalized_symbol, current_price)
    news = get_stock_news_sentiment(normalized_symbol)
    confluence = get_timeframe_confluence(normalized_symbol)

    best_setup = setup_matcher.get_best_setup(normalized_symbol)
    if not best_setup:
        return {"tradeable": False, "symbol": normalized_symbol, "reason": "No setup matched current conditions"}

    setup = dict(best_setup.get("setup_info") or best_setup.get("setup") or best_setup)
    setup["symbol"] = normalized_symbol
    entry = setup_matcher.calculate_entry_price(setup, candles)
    target = setup_matcher.calculate_target_price(setup, normalized_symbol, entry)
    stop = setup_matcher.calculate_stoploss_price(setup, normalized_symbol, entry)

    if entry <= 0 or target <= 0 or stop <= 0 or entry == stop:
        return {"tradeable": False, "symbol": normalized_symbol, "reason": "Invalid entry/target/stop calculation"}

    rr_ratio = abs(target - entry) / abs(entry - stop)
    if rr_ratio < MIN_RISK_REWARD:
        return {"tradeable": False, "symbol": normalized_symbol, "reason": f"R/R too low: {rr_ratio:.1f}:1"}

    signal_context = {
        "symbol": normalized_symbol,
        "direction": setup.get("direction_bias"),
        "market_condition": best_setup.get("data", {}).get("market_condition", "SIDEWAYS"),
        "timeframe_confluence": confluence,
    }
    features = ml_model.extract_features(normalized_symbol, signal_context, best_setup)
    ml_prediction = ml_model.predict(normalized_symbol, features, log_prediction=False)

    setup_confidence = (
        _normalize_ratio(best_setup.get("match_quality"))
        * _setup_win_rate(setup)
    )
    ml_confidence = _safe_float(ml_prediction.get("win_probability"), 0.5)
    if ml_prediction.get("fallback_used"):
        ml_confidence = 0.5
    news_confidence = (_safe_float(news.get("overall_sentiment")) + 1) / 2

    final_confidence = (
        (setup_confidence * 0.5)
        + (ml_confidence * 0.35)
        + (news_confidence * 0.15)
    )
    confidence_label = "HIGH" if final_confidence > 0.70 else "MEDIUM" if final_confidence > 0.55 else "LOW"
    tradeable = final_confidence > TRADEABLE_CONFIDENCE
    reasoning = build_reasoning_string(
        normalized_symbol,
        best_setup,
        indicators,
        patterns,
        levels,
        news,
        confluence,
        ml_prediction,
        entry,
        target,
        stop,
        rr_ratio,
    )

    weighted_score = round(final_confidence * 100, 2)
    return {
        "tradeable": tradeable,
        "symbol": normalized_symbol,
        "direction": str(setup.get("direction_bias", "BUY")).upper(),
        "entry_price": _round_price(entry),
        "target_price": _round_price(target),
        "stop_loss": _round_price(stop),
        "stop_loss_price": _round_price(stop),
        "risk_reward": round(rr_ratio, 2),
        "setup_used": setup.get("setup_name"),
        "setup_name": setup.get("setup_name"),
        "setup_code": setup.get("setup_code"),
        "setup_match": best_setup,
        "setup_match_quality": best_setup.get("match_quality"),
        "confidence": round(final_confidence, 4),
        "confidence_label": confidence_label,
        "weighted_score": weighted_score,
        "confidence_score": weighted_score,
        "ml_win_probability": _safe_float(ml_prediction.get("win_probability")),
        "news_sentiment": _safe_float(news.get("overall_sentiment")),
        "timeframe_confluence": confluence.get("confluence_level", "LOW"),
        "timeframe_confluence_detail": confluence,
        "market_condition": signal_context["market_condition"],
        "reasoning": reasoning,
        "explanation": reasoning,
        "patterns_found": [p.get("pattern_name") or p.get("name") for p in patterns if p.get("pattern_name") or p.get("name")],
        "key_level_used": levels,
        "ml_top_factors": ml_prediction.get("top_factors", []),
        "rsi_score": None,
        "macd_score": None,
        "volume_score": None,
        "vwap_score": None,
    }


def build_all_trade_plans() -> list[dict[str, Any]]:
    """Build trade plans for all latest Stage 2 candidates."""
    plans: list[dict[str, Any]] = []
    for symbol in _stage2_candidates():
        try:
            plan = build_trade_plan(symbol)
        except Exception as exc:
            log.exception("Trade-plan build failed for %s", symbol)
            plan = {"tradeable": False, "symbol": symbol, "reason": str(exc)}
        if plan.get("tradeable"):
            plans.append(plan)

    plans.sort(key=lambda item: _safe_float(item.get("confidence")), reverse=True)
    return plans


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    for trade_plan in build_all_trade_plans():
        print(trade_plan["reasoning"])
        print("-" * 64)
