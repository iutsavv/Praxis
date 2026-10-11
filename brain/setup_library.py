"""Seed and rank mathematically defined trade setups."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
log = logging.getLogger(__name__)


SETUPS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS setups (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_code              TEXT UNIQUE NOT NULL,
    setup_name              TEXT NOT NULL,
    setup_type              TEXT NOT NULL,
    direction_bias          TEXT NOT NULL,
    conditions_json         TEXT NOT NULL,
    entry_method            TEXT NOT NULL,
    target_method           TEXT NOT NULL,
    stoploss_method         TEXT NOT NULL,
    best_market_condition   TEXT,
    avoid_market_condition  TEXT,
    best_time_start         TEXT,
    best_time_end           TEXT,
    min_timeframe_confluence INTEGER DEFAULT 2,
    total_trades            INTEGER DEFAULT 0,
    win_rate                REAL DEFAULT 0,
    avg_win_pct             REAL DEFAULT 0,
    avg_loss_pct            REAL DEFAULT 0,
    profit_factor           REAL DEFAULT 0,
    best_stock              TEXT,
    is_active               INTEGER DEFAULT 1,
    created_at              TEXT DEFAULT (datetime('now')),
    last_updated            TEXT
)
"""


def _conditions(**kwargs: Any) -> str:
    return json.dumps(kwargs, sort_keys=True)


SETUPS: list[dict[str, Any]] = [
    {
        "setup_code": "RSI_OVERSOLD_SUPPORT",
        "setup_name": "RSI Oversold Bounce from Support",
        "setup_type": "reversal",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            rsi_max=35,
            distance_to_support_pct_max=0.5,
            volume_ratio_min=1.2,
            candlestick_pattern=["hammer", "bullish_engulfing", "morning_star"],
            timeframe_1h_bias="BULLISH",
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_resistance",
        "stoploss_method": "below_support",
        "best_market_condition": "TRENDING",
        "avoid_market_condition": "VOLATILE",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "MACD_CROSSOVER_UPTREND",
        "setup_name": "MACD Bullish Crossover in Uptrend",
        "setup_type": "momentum",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            macd_crossed_above_signal_bars=3,
            ema20_above_ema50=True,
            rsi_min=40,
            rsi_max=65,
            volume_ratio_min=1.0,
        ),
        "entry_method": "close_of_crossover_candle",
        "target_method": "previous_swing_high",
        "stoploss_method": "below_ema20",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "BREAKOUT_RESISTANCE",
        "setup_name": "Resistance Breakout",
        "setup_type": "breakout",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            price_above_resistance=True,
            volume_ratio_min=1.5,
            rsi_min=50,
            rsi_max=70,
            macd_histogram_positive=True,
        ),
        "entry_method": "breakout_price",
        "target_method": "measured_move",
        "stoploss_method": "below_breakout_level",
        "best_market_condition": "TRENDING",
        "avoid_market_condition": "SIDEWAYS",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "BREAKDOWN_SUPPORT",
        "setup_name": "Support Breakdown",
        "setup_type": "breakdown",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            price_below_support=True,
            volume_ratio_min=1.5,
            rsi_min=30,
            rsi_max=50,
            macd_histogram_negative=True,
        ),
        "entry_method": "breakdown_price",
        "target_method": "measured_move",
        "stoploss_method": "above_breakdown_level",
        "best_market_condition": "TRENDING",
        "avoid_market_condition": "SIDEWAYS",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "BULL_FLAG",
        "setup_name": "Bull Flag Continuation",
        "setup_type": "continuation",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            prior_trend="UP",
            consolidation_bars_min=5,
            consolidation_bars_max=15,
            flag_slope_negative=True,
            breakout_above_flag=True,
            volume_ratio_min=1.3,
        ),
        "entry_method": "breakout_price",
        "target_method": "measured_move",
        "stoploss_method": "below_flag_low",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:30",
        "best_time_end": "13:00",
    },
    {
        "setup_code": "BEAR_FLAG",
        "setup_name": "Bear Flag Continuation",
        "setup_type": "continuation",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            prior_trend="DOWN",
            consolidation_bars_min=5,
            consolidation_bars_max=15,
            flag_slope_positive=True,
            breakdown_below_flag=True,
            volume_ratio_min=1.3,
        ),
        "entry_method": "breakdown_price",
        "target_method": "measured_move",
        "stoploss_method": "above_flag_high",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:30",
        "best_time_end": "13:00",
    },
    {
        "setup_code": "MORNING_STAR_SUPPORT",
        "setup_name": "Morning Star at Support",
        "setup_type": "reversal",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            candlestick_pattern=["morning_star"],
            distance_to_support_pct_max=1.0,
            prior_trend="DOWN",
            volume_ratio_min=1.0,
            rsi_max=40,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_resistance",
        "stoploss_method": "below_support",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "EVENING_STAR_RESISTANCE",
        "setup_name": "Evening Star at Resistance",
        "setup_type": "reversal",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            candlestick_pattern=["evening_star"],
            distance_to_resistance_pct_max=1.0,
            prior_trend="UP",
            volume_ratio_min=1.0,
            rsi_min=60,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_support",
        "stoploss_method": "above_resistance",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "HAMMER_SUPPORT",
        "setup_name": "Hammer at Support",
        "setup_type": "reversal",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            candlestick_pattern=["hammer"],
            distance_to_support_pct_max=0.8,
            prior_trend="DOWN",
            volume_confirmed=True,
            rsi_max=40,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_resistance",
        "stoploss_method": "below_signal_candle",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "SHOOTING_STAR_RESISTANCE",
        "setup_name": "Shooting Star at Resistance",
        "setup_type": "reversal",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            candlestick_pattern=["shooting_star"],
            distance_to_resistance_pct_max=0.8,
            prior_trend="UP",
            volume_confirmed=True,
            rsi_min=60,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_support",
        "stoploss_method": "above_signal_candle",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "BULLISH_ENGULFING_SUPPORT",
        "setup_name": "Bullish Engulfing at Support",
        "setup_type": "reversal",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            candlestick_pattern=["bullish_engulfing"],
            distance_to_support_pct_max=1.0,
            volume_ratio_min=1.2,
            rsi_max=45,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_resistance",
        "stoploss_method": "below_support",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "13:00",
    },
    {
        "setup_code": "BEARISH_ENGULFING_RESISTANCE",
        "setup_name": "Bearish Engulfing at Resistance",
        "setup_type": "reversal",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            candlestick_pattern=["bearish_engulfing"],
            distance_to_resistance_pct_max=1.0,
            volume_ratio_min=1.2,
            rsi_min=55,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_support",
        "stoploss_method": "above_resistance",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "13:00",
    },
    {
        "setup_code": "DOUBLE_BOTTOM",
        "setup_name": "Double Bottom Reversal",
        "setup_type": "reversal",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            two_swing_lows_within_pct_max=1.5,
            bars_between_lows_min=10,
            bars_between_lows_max=50,
            bullish_divergence=True,
            volume_ratio_at_second_low_min=1.0,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "neckline_projection",
        "stoploss_method": "below_double_bottom",
        "best_market_condition": "TRENDING",
        "avoid_market_condition": "VOLATILE",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "DOUBLE_TOP",
        "setup_name": "Double Top Reversal",
        "setup_type": "reversal",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            two_swing_highs_within_pct_max=1.5,
            bars_between_highs_min=10,
            bars_between_highs_max=50,
            bearish_divergence=True,
            volume_ratio_at_second_high_min=1.0,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "neckline_projection",
        "stoploss_method": "above_double_top",
        "best_market_condition": "TRENDING",
        "avoid_market_condition": "VOLATILE",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "ASCENDING_TRIANGLE",
        "setup_name": "Ascending Triangle Breakout",
        "setup_type": "breakout",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            flat_resistance_touches_min=2,
            higher_lows_count_min=2,
            breakout_above_resistance=True,
            volume_ratio_min=1.5,
            rsi_min=50,
            rsi_max=65,
        ),
        "entry_method": "breakout_price",
        "target_method": "measured_move",
        "stoploss_method": "below_last_higher_low",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "13:00",
    },
    {
        "setup_code": "DESCENDING_TRIANGLE",
        "setup_name": "Descending Triangle Breakdown",
        "setup_type": "breakdown",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            flat_support_touches_min=2,
            lower_highs_count_min=2,
            breakdown_below_support=True,
            volume_ratio_min=1.5,
            rsi_min=35,
            rsi_max=50,
        ),
        "entry_method": "breakdown_price",
        "target_method": "measured_move",
        "stoploss_method": "above_last_lower_high",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "13:00",
    },
    {
        "setup_code": "VWAP_BOUNCE",
        "setup_name": "VWAP Bounce",
        "setup_type": "mean_reversion",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            price_near_vwap_pct=0.3,
            price_above_vwap=True,
            rsi_min=40,
            rsi_max=55,
            ema20_above_ema50=True,
            volume_ratio_min=0.8,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "fixed_pct_1_5",
        "stoploss_method": "below_vwap",
        "best_market_condition": "SIDEWAYS",
        "best_time_start": "09:30",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "VWAP_REJECTION",
        "setup_name": "VWAP Rejection",
        "setup_type": "mean_reversion",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            price_near_vwap_pct=0.3,
            price_below_vwap=True,
            rsi_min=45,
            rsi_max=60,
            ema20_below_ema50=True,
            volume_ratio_min=0.8,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "fixed_pct_1_5",
        "stoploss_method": "above_vwap",
        "best_market_condition": "SIDEWAYS",
        "best_time_start": "09:30",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "EMA20_BOUNCE",
        "setup_name": "EMA20 Trend Bounce",
        "setup_type": "trend_following",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            price_near_ema20_pct=0.5,
            price_above_ema20=True,
            ema20_above_ema50=True,
            rsi_min=45,
            rsi_max=65,
            macd_histogram_positive=True,
            volume_ratio_min=0.8,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "previous_swing_high",
        "stoploss_method": "below_ema20",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "EMA50_BOUNCE",
        "setup_name": "EMA50 Trend Bounce",
        "setup_type": "trend_following",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            price_near_ema50_pct=0.8,
            price_above_ema50=True,
            ema20_above_ema50=True,
            rsi_min=40,
            rsi_max=60,
            volume_ratio_min=1.0,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "previous_swing_high",
        "stoploss_method": "below_ema50",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
    {
        "setup_code": "NEWS_CATALYST_TECHNICAL",
        "setup_name": "News Catalyst with Technical Confirmation",
        "setup_type": "catalyst",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            news_sentiment_score_min=0.5,
            volume_ratio_min=2.0,
            rsi_min=40,
            rsi_max=70,
            price_above_ema20=True,
            announcement_boost_min=5.0,
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "fixed_pct_2",
        "stoploss_method": "fixed_pct_0_75",
        "best_time_start": "09:15",
        "best_time_end": "15:00",
    },
    {
        "setup_code": "VOLUME_CLIMAX_REVERSAL",
        "setup_name": "Volume Climax Reversal",
        "setup_type": "reversal",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            volume_ratio_min=3.0,
            rsi_max=30,
            distance_to_support_pct_max=1.0,
            candlestick_pattern=["hammer", "bullish_engulfing", "morning_star"],
            prior_trend="DOWN",
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_resistance",
        "stoploss_method": "below_support",
        "best_market_condition": "VOLATILE",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "OPENING_RANGE_BREAKOUT",
        "setup_name": "Opening Range Breakout",
        "setup_type": "breakout",
        "direction_bias": "BUY",
        "conditions_json": _conditions(
            time_since_open_minutes_max=30,
            price_above_opening_range_high=True,
            volume_ratio_min=1.5,
            rsi_min=50,
            rsi_max=70,
        ),
        "entry_method": "breakout_price",
        "target_method": "fixed_pct_1_5",
        "stoploss_method": "below_opening_range_low",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "10:00",
    },
    {
        "setup_code": "RSI_OVERBOUGHT_RESISTANCE",
        "setup_name": "RSI Overbought Rejection from Resistance",
        "setup_type": "reversal",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            rsi_min=70,
            distance_to_resistance_pct_max=0.5,
            volume_ratio_min=1.2,
            candlestick_pattern=["shooting_star", "bearish_engulfing", "evening_star"],
            timeframe_1h_bias="BEARISH",
        ),
        "entry_method": "close_of_signal_candle",
        "target_method": "nearest_support",
        "stoploss_method": "above_resistance",
        "best_market_condition": "TRENDING",
        "avoid_market_condition": "VOLATILE",
        "best_time_start": "09:15",
        "best_time_end": "11:30",
    },
    {
        "setup_code": "MACD_BEARISH_CROSSOVER",
        "setup_name": "MACD Bearish Crossover in Downtrend",
        "setup_type": "momentum",
        "direction_bias": "SELL",
        "conditions_json": _conditions(
            macd_crossed_below_signal_bars=3,
            ema20_below_ema50=True,
            rsi_min=35,
            rsi_max=60,
            volume_ratio_min=1.0,
        ),
        "entry_method": "close_of_crossover_candle",
        "target_method": "previous_swing_low",
        "stoploss_method": "above_ema20",
        "best_market_condition": "TRENDING",
        "best_time_start": "09:15",
        "best_time_end": "14:00",
    },
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_setups_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SETUPS_TABLE_SQL)
    conn.commit()


def _setup_params(setup: dict[str, Any]) -> dict[str, Any]:
    return {
        "avoid_market_condition": None,
        "best_market_condition": None,
        "best_time_start": None,
        "best_time_end": None,
        "min_timeframe_confluence": 2,
        **setup,
    }


def seed_initial_setups() -> dict[str, int]:
    """Insert all 25 default setups idempotently."""
    inserted = 0
    skipped = 0
    with _connect() as conn:
        ensure_setups_schema(conn)
        for setup in SETUPS:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO setups (
                    setup_code, setup_name, setup_type, direction_bias,
                    conditions_json, entry_method, target_method,
                    stoploss_method, best_market_condition,
                    avoid_market_condition, best_time_start, best_time_end,
                    min_timeframe_confluence
                ) VALUES (
                    :setup_code, :setup_name, :setup_type, :direction_bias,
                    :conditions_json, :entry_method, :target_method,
                    :stoploss_method, :best_market_condition,
                    :avoid_market_condition, :best_time_start, :best_time_end,
                    :min_timeframe_confluence
                )
                """,
                _setup_params(setup),
            )
            if cursor.rowcount:
                inserted += 1
                log.info("Inserted setup: %s", setup["setup_code"])
            else:
                skipped += 1
        conn.commit()
    return {"inserted": inserted, "skipped": skipped, "total": len(SETUPS)}


def _setup_identifier_expression(trade_columns: set[str], signal_columns: set[str]) -> str:
    candidates = []
    if "setup_code" in trade_columns:
        candidates.append("t.setup_code")
    if "setup_code" in signal_columns:
        candidates.append("s.setup_code")
    if "entry_reason" in trade_columns:
        candidates.append("t.entry_reason")
    if "notes" in signal_columns:
        candidates.append("s.notes")
    if "explanation" in signal_columns:
        candidates.append("s.explanation")
    return "COALESCE(" + ", ".join(candidates) + ")" if candidates else "NULL"


def _pnl_pct_expression(trade_columns: set[str]) -> str:
    if "pnl_pct" in trade_columns:
        return "t.pnl_pct"
    if "capital_required" in trade_columns:
        return "CASE WHEN t.capital_required > 0 THEN (t.pnl / t.capital_required) * 100 ELSE NULL END"
    return """
        CASE
            WHEN t.entry_price > 0 AND t.quantity > 0
            THEN (t.pnl / (t.entry_price * t.quantity)) * 100
            ELSE NULL
        END
    """


def update_setup_stats() -> dict[str, Any]:
    """Recalculate setup performance from closed paper trades."""
    summary: dict[str, Any] = {"updated": 0, "deactivated": 0, "errors": []}
    with _connect() as conn:
        ensure_setups_schema(conn)
        if not _table_exists(conn, "paper_trades"):
            return summary

        trade_columns = _columns(conn, "paper_trades")
        signal_columns = _columns(conn, "signal_scores")
        setup_identifier = _setup_identifier_expression(trade_columns, signal_columns)
        pnl_pct = _pnl_pct_expression(trade_columns)
        signal_join = """
            LEFT JOIN signal_scores s
              ON UPPER(s.symbol) = UPPER(t.symbol)
             AND datetime(s.created_at) <= datetime(t.entry_time)
             AND datetime(s.created_at) >= datetime(t.entry_time, '-2 days')
        """ if _table_exists(conn, "signal_scores") else "LEFT JOIN (SELECT NULL AS setup_code, NULL AS notes, NULL AS explanation, NULL AS created_at, NULL AS symbol) s ON 0"

        setups = conn.execute("SELECT setup_code, setup_name FROM setups").fetchall()
        for setup in setups:
            setup_code = str(setup["setup_code"])
            setup_name = str(setup["setup_name"])
            try:
                rows = conn.execute(
                    f"""
                    WITH matched_trades AS (
                        SELECT t.id, t.symbol, t.outcome, t.pnl, {pnl_pct} AS pnl_pct,
                               {setup_identifier} AS setup_identifier
                        FROM paper_trades t
                        {signal_join}
                        WHERE t.status = 'CLOSED'
                          AND t.outcome IN ('WIN', 'LOSS', 'BREAKEVEN')
                    )
                    SELECT *
                    FROM matched_trades
                    WHERE UPPER(COALESCE(setup_identifier, '')) IN (UPPER(?), UPPER(?))
                       OR UPPER(COALESCE(setup_identifier, '')) LIKE UPPER(?)
                    GROUP BY id
                    """,
                    (setup_code, setup_name, f"%{setup_code}%"),
                ).fetchall()

                total_trades = len(rows)
                if total_trades == 0:
                    continue

                wins = [row for row in rows if row["outcome"] == "WIN"]
                losses = [row for row in rows if row["outcome"] == "LOSS"]
                win_rate = round(len(wins) / total_trades * 100, 2)
                avg_win_pct = round(sum(float(row["pnl_pct"] or 0) for row in wins) / len(wins), 4) if wins else 0.0
                avg_loss_pct = round(sum(float(row["pnl_pct"] or 0) for row in losses) / len(losses), 4) if losses else 0.0
                gross_win = sum(max(0.0, float(row["pnl"] or 0)) for row in rows)
                gross_loss = abs(sum(min(0.0, float(row["pnl"] or 0)) for row in rows))
                profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else (round(gross_win, 4) if gross_win > 0 else 0.0)
                stock_totals: dict[str, float] = {}
                for row in rows:
                    stock = str(row["symbol"])
                    stock_totals[stock] = stock_totals.get(stock, 0.0) + float(row["pnl"] or 0)
                best_stock = max(stock_totals.items(), key=lambda item: item[1])[0] if stock_totals else None
                is_active = 0 if total_trades < 5 and win_rate < 40 else 1
                if is_active == 0:
                    summary["deactivated"] += 1

                conn.execute(
                    """
                    UPDATE setups
                    SET total_trades = ?,
                        win_rate = ?,
                        avg_win_pct = ?,
                        avg_loss_pct = ?,
                        profit_factor = ?,
                        best_stock = ?,
                        is_active = ?,
                        last_updated = ?
                    WHERE setup_code = ?
                    """,
                    (
                        total_trades,
                        win_rate,
                        avg_win_pct,
                        avg_loss_pct,
                        profit_factor,
                        best_stock,
                        is_active,
                        datetime.now().replace(microsecond=0).isoformat(),
                        setup_code,
                    ),
                )
                summary["updated"] += 1
            except Exception as exc:
                summary["errors"].append({"setup_code": setup_code, "error": str(exc)})

        conn.commit()
    return summary


def get_best_setup_for_conditions(market_condition: str, time_now: str) -> list[dict[str, Any]]:
    """Return active setups suitable for the market condition and clock time."""
    normalized_market = market_condition.upper().strip()
    with _connect() as conn:
        ensure_setups_schema(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM setups
            WHERE is_active = 1
              AND (best_market_condition IS NULL OR best_market_condition = ?)
              AND (avoid_market_condition IS NULL OR avoid_market_condition != ?)
              AND (best_time_start IS NULL OR best_time_start <= ?)
              AND (best_time_end IS NULL OR best_time_end >= ?)
            ORDER BY win_rate DESC, profit_factor DESC, total_trades DESC, setup_code ASC
            """,
            (normalized_market, normalized_market, time_now, time_now),
        ).fetchall()
    return [dict(row) for row in rows]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    print(seed_initial_setups())
