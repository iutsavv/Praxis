"""Shared memory and debate storage for trading agents.

The memory tables are intentionally generic: every agent can record what it
recommended, why, what evidence it used, and whether it later proved useful.
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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_text() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def ensure_memory_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create memory/debate tables when missing."""
    owns_connection = conn is None
    if conn is None:
        conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id              TEXT,
                symbol              TEXT,
                agent_name          TEXT NOT NULL,
                decision_type       TEXT NOT NULL,
                recommendation      TEXT NOT NULL,
                confidence          REAL DEFAULT 0,
                score_delta         REAL DEFAULT 0,
                hard_veto           INTEGER DEFAULT 0,
                reasons_json        TEXT,
                evidence_json       TEXT,
                related_trade_id    INTEGER,
                outcome             TEXT,
                created_at          TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_debates (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id              TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                input_plan_json     TEXT,
                votes_json          TEXT,
                final_action        TEXT NOT NULL,
                approved            INTEGER NOT NULL DEFAULT 0,
                final_confidence    REAL DEFAULT 0,
                final_reason        TEXT,
                created_at          TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_reviews (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER NOT NULL,
                symbol              TEXT NOT NULL,
                outcome             TEXT,
                pnl                 REAL,
                pnl_pct             REAL,
                review_json         TEXT,
                created_at          TEXT NOT NULL,
                UNIQUE(trade_id)
            )
            """
        )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def decision(
    agent_name: str,
    symbol: str,
    recommendation: str,
    confidence: float,
    reasons: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    decision_type: str = "trade_vote",
    score_delta: float = 0.0,
    hard_veto: bool = False,
    run_id: str | None = None,
    related_trade_id: int | None = None,
) -> dict[str, Any]:
    """Build a consistent in-memory decision payload."""
    return {
        "agent_name": agent_name,
        "symbol": symbol.upper(),
        "decision_type": decision_type,
        "recommendation": recommendation.upper(),
        "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
        "score_delta": round(float(score_delta), 4),
        "hard_veto": bool(hard_veto),
        "reasons": reasons or [],
        "evidence": evidence or {},
        "run_id": run_id,
        "related_trade_id": related_trade_id,
    }


def log_decision(payload: dict[str, Any]) -> int:
    """Persist one agent decision and return its memory id."""
    with _connect() as conn:
        ensure_memory_schema(conn)
        cursor = conn.execute(
            """
            INSERT INTO agent_memory (
                run_id, symbol, agent_name, decision_type, recommendation,
                confidence, score_delta, hard_veto, reasons_json, evidence_json,
                related_trade_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("run_id"),
                str(payload.get("symbol", "")).upper(),
                payload["agent_name"],
                payload.get("decision_type", "trade_vote"),
                payload["recommendation"],
                float(payload.get("confidence", 0.0)),
                float(payload.get("score_delta", 0.0)),
                1 if payload.get("hard_veto") else 0,
                _json(payload.get("reasons", [])),
                _json(payload.get("evidence", {})),
                payload.get("related_trade_id"),
                _now_text(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def log_debate(
    run_id: str,
    symbol: str,
    input_plan: dict[str, Any],
    votes: list[dict[str, Any]],
    final_action: str,
    approved: bool,
    final_confidence: float,
    final_reason: str,
) -> int:
    """Persist a complete debate result."""
    with _connect() as conn:
        ensure_memory_schema(conn)
        cursor = conn.execute(
            """
            INSERT INTO agent_debates (
                run_id, symbol, input_plan_json, votes_json, final_action,
                approved, final_confidence, final_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol.upper(),
                _json(input_plan),
                _json(votes),
                final_action.upper(),
                1 if approved else 0,
                float(final_confidence),
                final_reason,
                _now_text(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def link_debate_to_trade(run_id: str | None, trade_id: int) -> None:
    """Attach an opened trade id to all memory rows from a debate run."""
    if not run_id:
        return
    with _connect() as conn:
        ensure_memory_schema(conn)
        conn.execute(
            """
            UPDATE agent_memory
            SET related_trade_id = ?
            WHERE run_id = ?
            """,
            (trade_id, run_id),
        )
        conn.commit()


def mark_trade_outcome(trade_id: int, outcome: str | None) -> None:
    """Backfill the outcome onto all agent memories linked to a trade."""
    if outcome is None:
        return
    with _connect() as conn:
        ensure_memory_schema(conn)
        conn.execute(
            """
            UPDATE agent_memory
            SET outcome = ?
            WHERE related_trade_id = ?
            """,
            (outcome, trade_id),
        )
        conn.commit()


def recent_agent_memory(symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Read recent agent memory rows."""
    with _connect() as conn:
        ensure_memory_schema(conn)
        if symbol:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_memory
                WHERE UPPER(symbol) = UPPER(?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_memory
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def recent_debates(symbol: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Read recent debate summaries."""
    with _connect() as conn:
        ensure_memory_schema(conn)
        if symbol:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_debates
                WHERE UPPER(symbol) = UPPER(?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM agent_debates
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]
