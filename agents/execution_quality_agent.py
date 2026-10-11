"""Execution Quality Agent.

Checks whether a candidate is still practical to enter: price extension,
liquidity, risk/reward, and entry drift.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DB_PATH = PROJECT_ROOT / "database" / "trading.db"

from agents.agent_memory import decision, log_decision


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def _latest_candle(symbol: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT close, high, low, volume, timestamp
            FROM candles
            WHERE UPPER(symbol) = UPPER(?) AND interval = '15m'
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    return dict(row) if row else {}


def analyze(plan: dict[str, Any], run_id: str | None = None, persist: bool = False) -> dict[str, Any]:
    symbol = str(plan.get("symbol", "")).upper()
    direction = str(plan.get("direction", "")).upper()
    candle = _latest_candle(symbol)
    entry = _safe_float(plan.get("entry_price"))
    target = _safe_float(plan.get("target_price"))
    stop = _safe_float(plan.get("stop_loss_price") or plan.get("stop_loss"))
    current_price = _safe_float(candle.get("close"), entry)
    volume = _safe_float(candle.get("volume"))

    reasons: list[str] = []
    score_delta = 0.0
    hard_veto = False
    recommendation = "PASS"

    rr_ratio = abs(target - entry) / abs(entry - stop) if entry and stop and entry != stop else 0.0
    drift_pct = abs(current_price - entry) / entry * 100 if entry else 999.0
    candle_range_pct = (
        (_safe_float(candle.get("high")) - _safe_float(candle.get("low"))) / current_price * 100
        if current_price else 0.0
    )

    if rr_ratio and rr_ratio < 1.3:
        hard_veto = True
        recommendation = "BLOCK_TRADE"
        score_delta = -25
        reasons.append(f"Risk/reward is too low ({rr_ratio:.2f}:1).")
    if drift_pct > 0.75:
        recommendation = "REDUCE_CONFIDENCE" if not hard_veto else recommendation
        score_delta = min(score_delta, -10)
        reasons.append(f"Current price has drifted {drift_pct:.2f}% from planned entry.")
    if candle_range_pct > 2.5:
        recommendation = "REDUCE_CONFIDENCE" if not hard_veto else recommendation
        score_delta = min(score_delta, -8)
        reasons.append(f"Latest candle range is extended ({candle_range_pct:.2f}%).")
    if volume <= 0:
        recommendation = "REDUCE_CONFIDENCE" if not hard_veto else recommendation
        score_delta = min(score_delta, -5)
        reasons.append("Latest candle has no usable volume.")
    if not reasons:
        recommendation = "SUPPORT_TRADE"
        score_delta = 5
        reasons.append("Entry quality is acceptable: drift, volatility, and R/R are within limits.")

    payload = decision(
        "Execution Quality Agent",
        symbol,
        recommendation,
        0.75 if candle else 0.45,
        reasons,
        {
            "entry_price": entry,
            "current_price": current_price,
            "target_price": target,
            "stop_loss_price": stop,
            "risk_reward": round(rr_ratio, 3),
            "entry_drift_pct": round(drift_pct, 4),
            "latest_candle_range_pct": round(candle_range_pct, 4),
            "latest_volume": volume,
            "direction": direction,
        },
        score_delta=score_delta,
        hard_veto=hard_veto,
        run_id=run_id,
    )
    if persist:
        log_decision(payload)
    return payload


if __name__ == "__main__":
    import sys

    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    print(json.dumps(analyze({"symbol": symbol_arg, "direction": "BUY"}), indent=2))
