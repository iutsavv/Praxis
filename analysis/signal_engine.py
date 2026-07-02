"""
signal_engine.py — Rule-based signal scoring engine for the AI Paper Trading Agent.

Single responsibility: read indicators from the database, score each stock
using configurable rules loaded from analysis/signal_config.json + strategy_rules
table, and write results to the signal_scores table.

No network calls.  All data comes from database/trading.db.

Usage:
    python analysis/signal_engine.py
"""

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "database", "trading.db")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "analysis", "signal_config.json")


# ---------------------------------------------------------------------------
# Default configuration (written to signal_config.json on first run)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "rsi": {
        "buy_thresholds": [
            {"max": 30, "score": 25},
            {"max": 40, "score": 20},
            {"max": 50, "score": 10},
        ],
        "sell_thresholds": [
            {"min": 70, "score": 25},
            {"min": 60, "score": 20},
            {"min": 50, "score": 10},
        ],
    },
    "macd": {
        "crossover_lookback": 3,
        "crossover_score": 25,
        "histogram_growing_score": 15,
    },
    "volume": {
        "thresholds": [
            {"min": 2.5, "score": 25},
            {"min": 2.0, "score": 20},
            {"min": 1.5, "score": 15},
            {"min": 1.0, "score": 5},
        ],
    },
    "vwap": {
        "strong_band_pct": 0.5,
        "neutral_band_pct": 0.1,
        "strong_score": 25,
        "mild_score": 15,
        "neutral_score": 10,
    },
    "hold_threshold": 40,
    "market_condition": {
        "ema_slope_flat_pct": 0.1,
        "lookback_candles": 10,
        "slope_window": 5,
    },
}


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Load scoring config from signal_config.json.
    Creates the file with defaults if it doesn't exist."""
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"  [INFO] Created default config at {CONFIG_PATH}")
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IndicatorRow:
    """Latest indicator values for a single symbol."""
    symbol: str
    rsi: float | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    ema_20: float | None
    ema_50: float | None
    vwap: float | None
    volume_ratio: float | None
    close: float | None       # latest close price from candles
    timestamp: str = ""


@dataclass
class SignalResult:
    """Scoring output for one stock."""
    symbol: str
    direction: str             # BUY / SELL / HOLD
    rsi_score: float = 0.0
    macd_score: float = 0.0
    volume_score: float = 0.0
    vwap_score: float = 0.0
    weighted_score: float = 0.0
    market_condition: str = "SIDEWAYS"
    explanation: str = ""
    notes: str = ""


@dataclass
class StrategyWeights:
    """Active weights and thresholds from strategy_rules."""
    weight_rsi: float = 0.25
    weight_macd: float = 0.25
    weight_volume: float = 0.25
    weight_vwap: float = 0.25
    min_score_to_trade: float = 60.0
    max_open_trades: int = 3


# ---------------------------------------------------------------------------
# Database readers
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_active_symbols(conn: sqlite3.Connection) -> list[str]:
    """Read active symbols from stocks table."""
    rows = conn.execute(
        "SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol"
    ).fetchall()
    return [r["symbol"] for r in rows]


def get_strategy_weights(conn: sqlite3.Connection) -> StrategyWeights:
    """Read active strategy weights."""
    row = conn.execute(
        """SELECT weight_rsi, weight_macd, weight_volume, weight_vwap,
                  min_score_to_trade, max_open_trades
           FROM strategy_rules WHERE is_active = 1
           ORDER BY version DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        print("  [WARN] No active strategy_rules found, using defaults.")
        return StrategyWeights()
    return StrategyWeights(
        weight_rsi=row["weight_rsi"],
        weight_macd=row["weight_macd"],
        weight_volume=row["weight_volume"],
        weight_vwap=row["weight_vwap"],
        min_score_to_trade=row["min_score_to_trade"],
        max_open_trades=int(row["max_open_trades"]),
    )


def get_latest_indicator(conn: sqlite3.Connection, symbol: str) -> IndicatorRow | None:
    """Read the most recent indicator row for a symbol, joined with
    the close price from candles."""
    row = conn.execute(
        """SELECT i.symbol, i.rsi, i.macd, i.macd_signal, i.macd_histogram,
                  i.ema_20, i.ema_50, i.vwap, i.volume_ratio, i.timestamp,
                  c.close
           FROM indicators i
           JOIN candles c ON c.id = i.candle_id
           WHERE i.symbol = ?
           ORDER BY i.timestamp DESC
           LIMIT 1""",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return IndicatorRow(
        symbol=row["symbol"],
        rsi=row["rsi"],
        macd=row["macd"],
        macd_signal=row["macd_signal"],
        macd_histogram=row["macd_histogram"],
        ema_20=row["ema_20"],
        ema_50=row["ema_50"],
        vwap=row["vwap"],
        volume_ratio=row["volume_ratio"],
        close=row["close"],
        timestamp=row["timestamp"],
    )


def get_recent_macd(
    conn: sqlite3.Connection, symbol: str, lookback: int
) -> list[dict[str, float | None]]:
    """Return the last N indicator rows (oldest first) for MACD crossover detection."""
    rows = conn.execute(
        """SELECT macd, macd_signal, macd_histogram
           FROM indicators
           WHERE symbol = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (symbol, lookback),
    ).fetchall()
    # Reverse so index 0 = oldest, index -1 = latest
    return [dict(r) for r in reversed(rows)]


def get_nifty_candles(
    conn: sqlite3.Connection, count: int
) -> list[dict[str, Any]]:
    """Return the last `count` indicator rows for NSEI (Nifty 50 index),
    joined with the candle close price.  Falls back to ^NSEI/NIFTY."""
    for symbol in ("NSEI", "^NSEI", "NIFTY"):
        rows = conn.execute(
            """SELECT c.close, i.ema_20
               FROM indicators i
               JOIN candles c ON c.id = i.candle_id
               WHERE i.symbol = ?
               ORDER BY i.timestamp DESC
               LIMIT ?""",
            (symbol, count),
        ).fetchall()
        if rows:
            return [dict(r) for r in reversed(rows)]

    # Fallback: no Nifty data available
    return []


# ---------------------------------------------------------------------------
# Market condition detector
# ---------------------------------------------------------------------------

def detect_market_condition(
    conn: sqlite3.Connection, cfg: dict[str, Any]
) -> str:
    """Determine market condition from Nifty index EMA20 slope.

    Returns 'TRENDING', 'SIDEWAYS', or 'VOLATILE'.
    If no Nifty data is available, defaults to 'SIDEWAYS'.
    """
    mc_cfg = cfg.get("market_condition", DEFAULT_CONFIG["market_condition"])
    lookback = mc_cfg.get("lookback_candles", 10)
    slope_window = mc_cfg.get("slope_window", 5)
    flat_pct = mc_cfg.get("ema_slope_flat_pct", 0.1)

    candles = get_nifty_candles(conn, lookback)

    if len(candles) < slope_window:
        return "SIDEWAYS"

    # Use ema_20 if available, otherwise fall back to close
    recent = candles[-slope_window:]
    values = []
    for c in recent:
        v = c.get("ema_20") or c.get("close")
        if v is not None:
            values.append(float(v))

    if len(values) < 2:
        return "SIDEWAYS"

    # Slope as percentage change from first to last value
    slope_pct = ((values[-1] - values[0]) / values[0]) * 100 if values[0] != 0 else 0

    if slope_pct > flat_pct:
        return "TRENDING"
    elif slope_pct < -flat_pct:
        return "VOLATILE"
    else:
        return "SIDEWAYS"


# ---------------------------------------------------------------------------
# Individual signal scorers
# ---------------------------------------------------------------------------

def score_rsi(
    rsi: float | None, direction: str, cfg: dict[str, Any]
) -> tuple[float, str]:
    """Score RSI (0-25).  Returns (score, reason_text)."""
    if rsi is None:
        return 0.0, "RSI -- no data -- score 0/25"

    rsi_cfg = cfg.get("rsi", DEFAULT_CONFIG["rsi"])
    score = 0.0

    if direction == "BUY":
        for t in rsi_cfg["buy_thresholds"]:
            if rsi < t["max"]:
                score = float(t["score"])
                break
        if score >= 20:
            reason = f"RSI oversold ({rsi:.1f}) -- score {score:.0f}/25"
        elif score > 0:
            reason = f"RSI mildly oversold ({rsi:.1f}) -- score {score:.0f}/25"
        else:
            reason = f"RSI neutral/overbought ({rsi:.1f}) -- score 0/25"
    else:  # SELL
        for t in rsi_cfg["sell_thresholds"]:
            if rsi > t["min"]:
                score = float(t["score"])
                break
        if score >= 20:
            reason = f"RSI overbought ({rsi:.1f}) -- score {score:.0f}/25"
        elif score > 0:
            reason = f"RSI mildly overbought ({rsi:.1f}) -- score {score:.0f}/25"
        else:
            reason = f"RSI neutral/oversold ({rsi:.1f}) -- score 0/25"

    return score, reason


def score_macd(
    recent_macd: list[dict[str, float | None]], cfg: dict[str, Any]
) -> tuple[float, str, str]:
    """Score MACD (0-25).  Returns (score, reason_text, direction_hint).

    direction_hint is 'BUY', 'SELL', or 'NEUTRAL'.
    """
    macd_cfg = cfg.get("macd", DEFAULT_CONFIG["macd"])
    crossover_score = float(macd_cfg.get("crossover_score", 25))
    hist_growing_score = float(macd_cfg.get("histogram_growing_score", 15))

    if not recent_macd or len(recent_macd) < 2:
        return 0.0, "MACD -- insufficient data -- score 0/25", "NEUTRAL"

    # Check for crossovers in the lookback window
    bullish_cross = False
    bearish_cross = False

    for i in range(1, len(recent_macd)):
        prev = recent_macd[i - 1]
        curr = recent_macd[i]

        prev_macd = prev.get("macd")
        prev_sig = prev.get("macd_signal")
        curr_macd = curr.get("macd")
        curr_sig = curr.get("macd_signal")

        if any(v is None for v in (prev_macd, prev_sig, curr_macd, curr_sig)):
            continue

        # Bullish: MACD crosses above signal
        if prev_macd <= prev_sig and curr_macd > curr_sig:
            bullish_cross = True
        # Bearish: MACD crosses below signal
        if prev_macd >= prev_sig and curr_macd < curr_sig:
            bearish_cross = True

    if bullish_cross:
        return crossover_score, f"Bullish MACD crossover -- score {crossover_score:.0f}/25", "BUY"
    if bearish_cross:
        return crossover_score, f"Bearish MACD crossover -- score {crossover_score:.0f}/25", "SELL"

    # No crossover — check histogram direction
    latest = recent_macd[-1]
    prev = recent_macd[-2]
    latest_hist = latest.get("macd_histogram")
    prev_hist = prev.get("macd_histogram")

    if latest_hist is not None and prev_hist is not None:
        if abs(latest_hist) > abs(prev_hist):
            hint = "BUY" if latest_hist > 0 else "SELL"
            return (
                hist_growing_score,
                f"MACD histogram growing ({latest_hist:.4f}) -- score {hist_growing_score:.0f}/25",
                hint,
            )

    return 0.0, "MACD -- no crossover, flat histogram -- score 0/25", "NEUTRAL"


def score_volume(
    volume_ratio: float | None, cfg: dict[str, Any]
) -> tuple[float, str]:
    """Score volume (0-25).  Direction-neutral."""
    if volume_ratio is None:
        return 0.0, "Volume -- no data -- score 0/25"

    vol_cfg = cfg.get("volume", DEFAULT_CONFIG["volume"])
    score = 0.0

    for t in vol_cfg["thresholds"]:
        if volume_ratio > t["min"]:
            score = float(t["score"])
            break

    if score > 0:
        reason = f"Volume spike {volume_ratio:.1f}x average -- score {score:.0f}/25"
    else:
        reason = f"Volume below average ({volume_ratio:.1f}x) -- score 0/25"

    return score, reason


def score_vwap(
    close: float | None, vwap: float | None, direction: str, cfg: dict[str, Any]
) -> tuple[float, str]:
    """Score VWAP (0-25).  Direction-dependent."""
    if close is None or vwap is None or vwap == 0:
        return 0.0, "VWAP -- no data -- score 0/25"

    vwap_cfg = cfg.get("vwap", DEFAULT_CONFIG["vwap"])
    strong_band = vwap_cfg.get("strong_band_pct", 0.5)
    neutral_band = vwap_cfg.get("neutral_band_pct", 0.1)
    strong_score = float(vwap_cfg.get("strong_score", 25))
    mild_score = float(vwap_cfg.get("mild_score", 15))
    neutral_score = float(vwap_cfg.get("neutral_score", 10))

    diff_pct = ((close - vwap) / vwap) * 100

    if direction == "BUY":
        if diff_pct > strong_band:
            return strong_score, f"Price above VWAP by {diff_pct:.2f}% -- score {strong_score:.0f}/25"
        elif diff_pct > 0:
            return mild_score, f"Price slightly above VWAP ({diff_pct:.2f}%) -- score {mild_score:.0f}/25"
        elif abs(diff_pct) <= neutral_band:
            return neutral_score, f"Price at VWAP ({diff_pct:+.2f}%) -- score {neutral_score:.0f}/25"
        else:
            return 0.0, f"VWAP -- price below VWAP ({diff_pct:.2f}%) -- score 0/25"
    else:  # SELL
        if diff_pct < -strong_band:
            return strong_score, f"Price below VWAP by {abs(diff_pct):.2f}% -- score {strong_score:.0f}/25"
        elif diff_pct < 0:
            return mild_score, f"Price slightly below VWAP ({diff_pct:.2f}%) -- score {mild_score:.0f}/25"
        elif abs(diff_pct) <= neutral_band:
            return neutral_score, f"Price at VWAP ({diff_pct:+.2f}%) -- score {neutral_score:.0f}/25"
        else:
            return 0.0, f"VWAP -- price above VWAP ({diff_pct:+.2f}%) -- score 0/25"


# ---------------------------------------------------------------------------
# Core scoring logic
# ---------------------------------------------------------------------------

def score_stock(
    conn: sqlite3.Connection,
    symbol: str,
    weights: StrategyWeights,
    market_condition: str,
    cfg: dict[str, Any],
) -> SignalResult | None:
    """Score a single stock.  Returns None if no indicator data exists."""

    ind = get_latest_indicator(conn, symbol)
    if ind is None:
        return None

    macd_cfg = cfg.get("macd", DEFAULT_CONFIG["macd"])
    lookback = macd_cfg.get("crossover_lookback", 3)
    recent_macd = get_recent_macd(conn, symbol, lookback)

    hold_threshold = cfg.get("hold_threshold", DEFAULT_CONFIG["hold_threshold"])

    # ── Step 1: Score MACD to get direction hint ─────────────────
    macd_sc, macd_reason, macd_hint = score_macd(recent_macd, cfg)

    # ── Step 2: Determine primary direction ──────────────────────
    if macd_hint == "NEUTRAL":
        # Fall back to RSI
        if ind.rsi is not None and ind.rsi < 50:
            primary_dir = "BUY"
        else:
            primary_dir = "SELL"
    else:
        primary_dir = macd_hint

    # ── Step 3: Score RSI + VWAP in both directions, pick best ──
    rsi_buy, rsi_buy_reason = score_rsi(ind.rsi, "BUY", cfg)
    rsi_sell, rsi_sell_reason = score_rsi(ind.rsi, "SELL", cfg)
    vwap_buy, vwap_buy_reason = score_vwap(ind.close, ind.vwap, "BUY", cfg)
    vwap_sell, vwap_sell_reason = score_vwap(ind.close, ind.vwap, "SELL", cfg)
    vol_sc, vol_reason = score_volume(ind.volume_ratio, cfg)

    buy_direction_score = (
        rsi_buy * weights.weight_rsi
        + (macd_sc if macd_hint == "BUY" else 0.0) * weights.weight_macd
        + vol_sc * weights.weight_volume
        + vwap_buy * weights.weight_vwap
    )
    sell_direction_score = (
        rsi_sell * weights.weight_rsi
        + (macd_sc if macd_hint == "SELL" else 0.0) * weights.weight_macd
        + vol_sc * weights.weight_volume
        + vwap_sell * weights.weight_vwap
    )
    best_scored_direction = "BUY" if buy_direction_score >= sell_direction_score else "SELL"

    # Keep direction aligned with the MACD hint (or RSI fallback) calculated
    # above. Previously primary_dir was unused, allowing a bullish MACD score
    # to be counted toward a contradictory SELL signal.
    if primary_dir == "BUY":
        direction = "BUY"
        rsi_sc, rsi_reason = rsi_buy, rsi_buy_reason
        vwap_sc, vwap_reason = vwap_buy, vwap_buy_reason
    else:
        direction = "SELL"
        rsi_sc, rsi_reason = rsi_sell, rsi_sell_reason
        vwap_sc, vwap_reason = vwap_sell, vwap_sell_reason

    # ── Step 4: Calculate weighted score (0-100) ─────────────────
    # Each raw score is 0-25.  Weighted sum with weights summing to 1.0
    # gives 0-25.  Scale to 0-100 by multiplying by 4.
    raw_weighted = (
        rsi_sc * weights.weight_rsi
        + macd_sc * weights.weight_macd
        + vol_sc * weights.weight_volume
        + vwap_sc * weights.weight_vwap
    )
    weighted_score = min(round(raw_weighted * 4, 2), 100.0)

    boost_reasons: list[str] = []
    if 35 <= weighted_score < hold_threshold:
        if ind.rsi is not None and ind.rsi < 35:
            weighted_score = min(weighted_score + 8, 100.0)
            boost_reasons.append("oversold_boost")
        elif ind.rsi is not None and ind.rsi > 70:
            weighted_score = min(weighted_score + 8, 100.0)
            boost_reasons.append("overbought_boost")
        if ind.volume_ratio is not None and ind.volume_ratio > 2.0:
            weighted_score = min(weighted_score + 5, 100.0)
            boost_reasons.append("volume_confirmation")
        weighted_score = round(weighted_score, 2)

    # ── Step 5: Apply HOLD threshold ─────────────────────────────
    if weighted_score < hold_threshold:
        direction = "HOLD"
    else:
        direction = best_scored_direction
        if direction == "BUY":
            rsi_sc, rsi_reason = rsi_buy, rsi_buy_reason
            vwap_sc, vwap_reason = vwap_buy, vwap_buy_reason
        else:
            rsi_sc, rsi_reason = rsi_sell, rsi_sell_reason
            vwap_sc, vwap_reason = vwap_sell, vwap_sell_reason

    # ── Step 6: Build explanation ────────────────────────────────
    lines = [f"{direction} because:" if direction != "HOLD" else "HOLD -- score below threshold:"]

    for sc, reason in [
        (rsi_sc, rsi_reason),
        (macd_sc, macd_reason),
        (vol_sc, vol_reason),
        (vwap_sc, vwap_reason),
    ]:
        marker = "[+]" if sc > 0 else "[x]"
        lines.append(f"  {marker} {reason}")

    lines.append(f"  Market condition: {market_condition}")
    if boost_reasons:
        lines.append(f"  Secondary boost: {', '.join(boost_reasons)}")
    explanation = "\n".join(lines)
    notes = ",".join(boost_reasons)

    return SignalResult(
        symbol=symbol,
        direction=direction,
        rsi_score=rsi_sc,
        macd_score=macd_sc,
        volume_score=vol_sc,
        vwap_score=vwap_sc,
        weighted_score=weighted_score,
        market_condition=market_condition,
        explanation=explanation,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Database writer
# ---------------------------------------------------------------------------

def write_signal_scores(
    conn: sqlite3.Connection,
    results: list[SignalResult],
    scan_cycle: str,
) -> int:
    """Insert signal score rows.  Returns count of rows inserted."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(signal_scores)")}
    if "notes" not in columns:
        conn.execute("ALTER TABLE signal_scores ADD COLUMN notes TEXT")
        conn.commit()

    cursor = conn.cursor()
    count = 0

    for r in results:
        cursor.execute(
            """INSERT INTO signal_scores
                   (symbol, scan_cycle, confidence_score, direction,
                    market_condition, contrib_rsi, contrib_macd,
                    contrib_volume, contrib_vwap, explanation, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.symbol,
                scan_cycle,
                r.weighted_score,
                r.direction,
                r.market_condition,
                r.rsi_score,
                r.macd_score,
                r.volume_score,
                r.vwap_score,
                r.explanation,
                r.notes,
            ),
        )
        count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Top picks filter
# ---------------------------------------------------------------------------

def get_top_picks(
    results: list[SignalResult],
    min_score: float,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Filter, sort, and return the top N actionable signals."""
    actionable = [
        r for r in results
        if r.direction != "HOLD" and r.weighted_score >= min_score
    ]
    actionable.sort(key=lambda r: r.weighted_score, reverse=True)

    return [
        {
            "symbol": r.symbol,
            "direction": r.direction,
            "weighted_score": r.weighted_score,
            "rsi_score": r.rsi_score,
            "macd_score": r.macd_score,
            "volume_score": r.volume_score,
            "vwap_score": r.vwap_score,
            "market_condition": r.market_condition,
            "explanation": r.explanation,
        }
        for r in actionable[:top_n]
    ]


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

def print_scan_results(
    results: list[SignalResult],
    picks: list[dict[str, Any]],
    errors: int,
    scan_time: str,
) -> None:
    """Print a clean ranked summary table."""
    scanned = len(results)
    above = len(picks)

    print(f"\n-- SCAN RESULTS {scan_time} IST " + "-" * 30)
    print(f"  {'Rank':<6}{'Symbol':<14}{'Direction':<12}{'Score':>6}  {'Market'}")

    for i, p in enumerate(picks, start=1):
        print(
            f"  {i:<6}{p['symbol']:<14}{p['direction']:<12}"
            f"{p['weighted_score']:>6.1f}  {p['market_condition']}"
        )

    if not picks:
        print("  (no stocks above threshold)")

    print("-" * 52)
    print(
        f"  {scanned} stocks scanned. "
        f"{above} above threshold. "
        f"{errors} errors."
    )
    print("-" * 52 + "\n")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_scan() -> list[dict[str, Any]]:
    """Run a full signal scan.  Returns top picks as a list of dicts."""

    print("\n" + "=" * 56)
    print("  AI Paper Trading Agent -- Signal Scoring Engine")
    print("=" * 56)

    cfg = load_config()
    conn = _get_connection()

    symbols = get_active_symbols(conn)
    weights = get_strategy_weights(conn)
    market_condition = detect_market_condition(conn, cfg)

    print(f"  Active symbols  : {len(symbols)}")
    print(f"  Market condition: {market_condition}")
    print(f"  Min score       : {weights.min_score_to_trade}")
    print(
        f"  Weights         : RSI={weights.weight_rsi:.2f}  "
        f"MACD={weights.weight_macd:.2f}  "
        f"VOL={weights.weight_volume:.2f}  "
        f"VWAP={weights.weight_vwap:.2f}"
    )
    print("=" * 56 + "\n")

    scan_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results: list[SignalResult] = []
    errors = 0

    for symbol in symbols:
        try:
            result = score_stock(conn, symbol, weights, market_condition, cfg)
            if result is None:
                print(f"  [SKIP] {symbol:<12} -- no indicator data")
                errors += 1
            else:
                tag = f"{result.direction:<5} {result.weighted_score:5.1f}"
                print(f"  [OK]   {symbol:<12} -- {tag}")
                results.append(result)
        except Exception as e:
            print(f"  [ERR]  {symbol:<12} -- {e}")
            errors += 1

    # Write all results to DB
    if results:
        written = write_signal_scores(conn, results, scan_cycle)
        print(f"\n  [DB] {written} signal scores written to database.")

    # Get top picks
    picks = get_top_picks(results, weights.min_score_to_trade, top_n=weights.max_open_trades)

    # Print summary
    scan_time = datetime.now().strftime("%H:%M")
    print_scan_results(results, picks, errors, scan_time)

    conn.close()
    return picks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    top = run_scan()

    if top:
        print("  TOP PICKS (detailed):")
        for i, pick in enumerate(top, 1):
            print(f"\n  --- #{i} {pick['symbol']} ---")
            print(f"  {pick['explanation']}")
    else:
        print("  No actionable signals in this scan.")
