"""Market Regime Agent.

Judges whether the broad market backdrop supports taking new trades.
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


def _index_momentum(conn: sqlite3.Connection) -> dict[str, float | str]:
    for symbol in ("NIFTY", "NSEI", "^NSEI", "NIFTY50"):
        rows = conn.execute(
            """
            SELECT close
            FROM candles
            WHERE UPPER(symbol) = UPPER(?) AND interval = '15m'
            ORDER BY timestamp DESC
            LIMIT 8
            """,
            (symbol,),
        ).fetchall()
        if len(rows) >= 2:
            latest = _safe_float(rows[0]["close"])
            earlier = _safe_float(rows[-1]["close"])
            return {
                "symbol": symbol,
                "momentum_pct": ((latest - earlier) / earlier * 100) if earlier else 0.0,
            }
    return {"symbol": "UNKNOWN", "momentum_pct": 0.0}


def _breadth(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT symbol, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
            FROM candles
            WHERE interval = '15m'
        )
        SELECT
            SUM(CASE WHEN latest.close > previous.close THEN 1 ELSE 0 END) AS advancers,
            SUM(CASE WHEN latest.close < previous.close THEN 1 ELSE 0 END) AS decliners,
            COUNT(*) AS total
        FROM ranked latest
        JOIN ranked previous ON previous.symbol = latest.symbol AND previous.rn = 2
        WHERE latest.rn = 1
        """
    ).fetchone()
    total = _safe_float(rows["total"] if rows else 0.0)
    advancers = _safe_float(rows["advancers"] if rows else 0.0)
    decliners = _safe_float(rows["decliners"] if rows else 0.0)
    return {
        "advancers": advancers,
        "decliners": decliners,
        "total": total,
        "advance_ratio": advancers / total if total else 0.5,
    }


def analyze(plan: dict[str, Any], run_id: str | None = None, persist: bool = False) -> dict[str, Any]:
    symbol = str(plan.get("symbol", "")).upper()
    direction = str(plan.get("direction", "")).upper()
    with _connect() as conn:
        momentum = _index_momentum(conn)
        breadth = _breadth(conn)

    index_momentum = _safe_float(momentum.get("momentum_pct"))
    advance_ratio = _safe_float(breadth.get("advance_ratio"), 0.5)
    score_delta = 0.0
    reasons: list[str] = []
    recommendation = "NEUTRAL"
    hard_veto = False

    if direction == "BUY":
        if index_momentum <= -0.8 and advance_ratio < 0.35:
            recommendation = "REDUCE_CONFIDENCE"
            score_delta = -12
            reasons.append("Broad market is weak while candidate is a long trade.")
        elif index_momentum >= 0.4 and advance_ratio > 0.55:
            recommendation = "SUPPORT_TRADE"
            score_delta = 7
            reasons.append("Broad market momentum and breadth support long trades.")
    elif direction == "SELL":
        if index_momentum >= 0.8 and advance_ratio > 0.65:
            recommendation = "REDUCE_CONFIDENCE"
            score_delta = -10
            reasons.append("Broad market is strong while candidate is a short trade.")
        elif index_momentum <= -0.4 and advance_ratio < 0.45:
            recommendation = "SUPPORT_TRADE"
            score_delta = 7
            reasons.append("Broad market weakness supports short trades.")

    if not reasons:
        reasons.append("Market regime is not strongly directional.")

    payload = decision(
        "Market Regime Agent",
        symbol,
        recommendation,
        0.65,
        reasons,
        {"index_momentum": momentum, "breadth": breadth},
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
