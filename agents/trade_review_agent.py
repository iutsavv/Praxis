"""Trade Review Agent.

After a trade closes, this agent explains the outcome and stores a durable
review that can be used by the learning agent and future debates.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = timezone(timedelta(hours=5, minutes=30))

from agents.agent_memory import ensure_memory_schema


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def review_trade(trade_id: int, persist: bool = True) -> dict[str, Any]:
    """Review one closed trade."""
    with _connect() as conn:
        ensure_memory_schema(conn)
        trade = conn.execute(
            "SELECT * FROM paper_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        if trade is None:
            return {"success": False, "reason": "Trade not found"}

        memories = conn.execute(
            """
            SELECT agent_name, recommendation, confidence, score_delta,
                   hard_veto, reasons_json, created_at
            FROM agent_memory
            WHERE related_trade_id = ?
               OR (UPPER(symbol) = UPPER(?) AND created_at <= COALESCE(?, created_at))
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (trade_id, trade["symbol"], trade["exit_time"]),
        ).fetchall()

    trade_dict = dict(trade)
    outcome = str(trade_dict.get("outcome") or "OPEN").upper()
    pnl_pct = _safe_float(trade_dict.get("pnl_pct"))
    exit_reason = str(trade_dict.get("exit_reason") or "")
    lessons: list[str] = []

    if outcome == "WIN":
        lessons.append("Trade worked; preserve the setup evidence for similar future candidates.")
    elif exit_reason == "STOP_LOSS":
        lessons.append("Stopped out; future debates should inspect entry timing and contrary agent votes.")
    elif exit_reason == "EOD_EXIT":
        lessons.append("Exited at EOD; review whether intraday momentum faded before target.")
    elif outcome == "LOSS":
        lessons.append("Loss recorded; compare agent confidence with actual result before increasing exposure.")
    else:
        lessons.append("Trade is not closed yet; full review will be available after exit.")

    agent_votes = []
    for row in memories:
        try:
            reasons = json.loads(row["reasons_json"] or "[]")
        except json.JSONDecodeError:
            reasons = []
        agent_votes.append(
            {
                "agent_name": row["agent_name"],
                "recommendation": row["recommendation"],
                "confidence": row["confidence"],
                "score_delta": row["score_delta"],
                "hard_veto": bool(row["hard_veto"]),
                "reasons": reasons,
                "created_at": row["created_at"],
            }
        )

    review = {
        "success": True,
        "trade_id": trade_id,
        "symbol": trade_dict["symbol"],
        "outcome": outcome,
        "pnl": _safe_float(trade_dict.get("pnl")),
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
        "lessons": lessons,
        "agent_votes": agent_votes,
    }

    if persist and trade_dict.get("status") == "CLOSED":
        with _connect() as conn:
            ensure_memory_schema(conn)
            conn.execute(
                """
                INSERT INTO trade_reviews (
                    trade_id, symbol, outcome, pnl, pnl_pct, review_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    outcome = excluded.outcome,
                    pnl = excluded.pnl,
                    pnl_pct = excluded.pnl_pct,
                    review_json = excluded.review_json,
                    created_at = excluded.created_at
                """,
                (
                    trade_id,
                    trade_dict["symbol"],
                    outcome,
                    _safe_float(trade_dict.get("pnl")),
                    pnl_pct,
                    json.dumps(review, sort_keys=True, default=str),
                    datetime.now(IST).replace(microsecond=0).isoformat(),
                ),
            )
            conn.commit()
    return review


if __name__ == "__main__":
    import sys

    trade_id_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(json.dumps(review_trade(trade_id_arg), indent=2))
