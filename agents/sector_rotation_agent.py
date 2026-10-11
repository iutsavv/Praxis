"""Sector Rotation Agent.

Compares a stock's sector with the rest of the market and votes for or against
the candidate direction.
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


def _sector_for_symbol(conn: sqlite3.Connection, symbol: str) -> str:
    row = conn.execute(
        """
        SELECT COALESCE(su.sector, s.sector, 'Unknown') AS sector
        FROM stock_universe su
        LEFT JOIN stocks s ON UPPER(s.symbol) = UPPER(su.symbol)
        WHERE UPPER(su.symbol) = UPPER(?)
        UNION
        SELECT COALESCE(sector, 'Unknown') AS sector
        FROM stocks
        WHERE UPPER(symbol) = UPPER(?)
        LIMIT 1
        """,
        (symbol, symbol),
    ).fetchone()
    return str(row["sector"] if row else "Unknown")


def _sector_momentum(conn: sqlite3.Connection, sector: str) -> dict[str, float]:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT c.symbol, c.close,
                   ROW_NUMBER() OVER (PARTITION BY c.symbol ORDER BY c.timestamp DESC) AS rn
            FROM candles c
            JOIN stock_universe su ON UPPER(su.symbol) = UPPER(c.symbol)
            WHERE c.interval = '15m'
              AND COALESCE(su.sector, 'Unknown') = ?
        )
        SELECT AVG((latest.close - previous.close) / previous.close * 100) AS momentum_pct,
               COUNT(*) AS sample_size
        FROM ranked latest
        JOIN ranked previous ON previous.symbol = latest.symbol AND previous.rn = 4
        WHERE latest.rn = 1 AND previous.close > 0
        """,
        (sector,),
    ).fetchone()
    return {
        "momentum_pct": _safe_float(rows["momentum_pct"] if rows else 0.0),
        "sample_size": _safe_float(rows["sample_size"] if rows else 0.0),
    }


def analyze(plan: dict[str, Any], run_id: str | None = None, persist: bool = False) -> dict[str, Any]:
    symbol = str(plan.get("symbol", "")).upper()
    direction = str(plan.get("direction", "")).upper()
    with _connect() as conn:
        sector = _sector_for_symbol(conn, symbol)
        sector_data = _sector_momentum(conn, sector)

    momentum = _safe_float(sector_data.get("momentum_pct"))
    sample_size = _safe_float(sector_data.get("sample_size"))
    recommendation = "NEUTRAL"
    score_delta = 0.0
    reasons: list[str] = []

    if sample_size < 3:
        reasons.append(f"Not enough sector data for {sector}.")
    elif direction == "BUY" and momentum >= 0.35:
        recommendation = "SUPPORT_TRADE"
        score_delta = 6
        reasons.append(f"{sector} sector is outperforming short-term ({momentum:+.2f}%).")
    elif direction == "BUY" and momentum <= -0.35:
        recommendation = "REDUCE_CONFIDENCE"
        score_delta = -6
        reasons.append(f"{sector} sector is weak short-term ({momentum:+.2f}%).")
    elif direction == "SELL" and momentum <= -0.35:
        recommendation = "SUPPORT_TRADE"
        score_delta = 6
        reasons.append(f"{sector} sector weakness supports short trades ({momentum:+.2f}%).")
    elif direction == "SELL" and momentum >= 0.35:
        recommendation = "REDUCE_CONFIDENCE"
        score_delta = -6
        reasons.append(f"{sector} sector strength argues against short trades ({momentum:+.2f}%).")
    else:
        reasons.append(f"{sector} sector momentum is neutral ({momentum:+.2f}%).")

    payload = decision(
        "Sector Rotation Agent",
        symbol,
        recommendation,
        0.6 if sample_size >= 3 else 0.35,
        reasons,
        {"sector": sector, "sector_momentum": sector_data},
        score_delta=score_delta,
        run_id=run_id,
    )
    if persist:
        log_decision(payload)
    return payload


if __name__ == "__main__":
    import sys

    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    print(json.dumps(analyze({"symbol": symbol_arg, "direction": "BUY"}), indent=2))
