"""Fundamentals Analyst Agent.

Reads stored earnings/fundamental facts and corporate disclosures. For NSE
stocks this is designed around exchange announcements and future result PDFs;
for US-listed symbols it can later be fed SEC EDGAR facts by CIK.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = timezone(timedelta(hours=5, minutes=30))

from agents.agent_memory import decision, log_decision


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_fundamentals_schema(conn: sqlite3.Connection | None = None) -> None:
    owns_connection = conn is None
    if conn is None:
        conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamental_documents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                source          TEXT NOT NULL,
                document_type   TEXT,
                title           TEXT,
                url             TEXT,
                filing_date     TEXT,
                period_end      TEXT,
                raw_text        TEXT,
                parsed_at       TEXT,
                UNIQUE(symbol, source, url)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamental_facts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                period_end      TEXT,
                fiscal_period   TEXT,
                metric          TEXT NOT NULL,
                value           REAL,
                unit            TEXT,
                source_document_id INTEGER,
                created_at      TEXT NOT NULL,
                UNIQUE(symbol, period_end, fiscal_period, metric)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamental_agent_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                recommendation  TEXT NOT NULL,
                confidence      REAL DEFAULT 0,
                report_json     TEXT,
                created_at      TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def _latest_facts(conn: sqlite3.Connection, symbol: str) -> dict[str, float]:
    ensure_fundamentals_schema(conn)
    rows = conn.execute(
        """
        SELECT metric, value
        FROM fundamental_facts
        WHERE UPPER(symbol) = UPPER(?)
          AND period_end = (
              SELECT MAX(period_end)
              FROM fundamental_facts
              WHERE UPPER(symbol) = UPPER(?)
          )
        """,
        (symbol, symbol),
    ).fetchall()
    return {str(row["metric"]).lower(): _safe_float(row["value"]) for row in rows}


def _recent_disclosures(conn: sqlite3.Connection, symbol: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "announcements"):
        return []
    cutoff = (datetime.now(IST) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT announcement_type, headline, details, sentiment_score,
               signal_boost, published_at
        FROM announcements
        WHERE UPPER(symbol) = UPPER(?)
          AND COALESCE(published_at, fetched_at) >= ?
        ORDER BY COALESCE(published_at, fetched_at) DESC
        LIMIT 10
        """,
        (symbol, cutoff),
    ).fetchall()
    return [dict(row) for row in rows]


def analyze(plan: dict[str, Any], run_id: str | None = None, persist: bool = False) -> dict[str, Any]:
    """Vote on whether fundamentals support or block a candidate trade."""
    symbol = str(plan.get("symbol", "")).upper()
    with _connect() as conn:
        facts = _latest_facts(conn, symbol)
        disclosures = _recent_disclosures(conn, symbol)

    reasons: list[str] = []
    positives: list[str] = []
    negatives: list[str] = []
    hard_veto = False
    score_delta = 0.0

    revenue_growth = facts.get("revenue_growth_pct")
    profit_growth = facts.get("profit_growth_pct")
    margin_change = facts.get("margin_change_pct")
    debt_to_equity = facts.get("debt_to_equity")
    promoter_pledge = facts.get("promoter_pledge_pct")

    if revenue_growth is not None:
        if revenue_growth >= 10:
            positives.append(f"revenue growth {revenue_growth:.1f}%")
        elif revenue_growth <= -5:
            negatives.append(f"revenue decline {revenue_growth:.1f}%")
    if profit_growth is not None:
        if profit_growth >= 10:
            positives.append(f"profit growth {profit_growth:.1f}%")
        elif profit_growth <= -10:
            negatives.append(f"profit decline {profit_growth:.1f}%")
    if margin_change is not None:
        if margin_change >= 1:
            positives.append(f"margin expansion {margin_change:.1f}%")
        elif margin_change <= -2:
            negatives.append(f"margin compression {margin_change:.1f}%")
    if debt_to_equity is not None and debt_to_equity >= 2.0:
        negatives.append(f"high debt/equity {debt_to_equity:.2f}")
    if promoter_pledge is not None and promoter_pledge >= 20:
        negatives.append(f"high promoter pledge {promoter_pledge:.1f}%")

    disclosure_text = " ".join(
        f"{row.get('announcement_type', '')} {row.get('headline', '')} {row.get('details', '')}".lower()
        for row in disclosures
    )
    if any(term in disclosure_text for term in ("default", "fraud", "investigation", "regulatory action")):
        hard_veto = True
        negatives.append("recent high-risk corporate disclosure")
    if any(term in disclosure_text for term in ("financial results", "strong results", "record profit")):
        positives.append("recent result-related disclosure")

    if hard_veto:
        recommendation = "BLOCK_TRADE"
        score_delta = -30
    elif len(negatives) >= 2 and not positives:
        recommendation = "REDUCE_CONFIDENCE"
        score_delta = -12
    elif positives and not negatives:
        recommendation = "SUPPORT_TRADE"
        score_delta = 8
    elif positives and negatives:
        recommendation = "MIXED"
        score_delta = -2
    else:
        recommendation = "NO_DATA"
        reasons.append("No parsed earnings/fundamental facts available yet.")

    if positives:
        reasons.append("Positive factors: " + ", ".join(positives[:4]) + ".")
    if negatives:
        reasons.append("Negative factors: " + ", ".join(negatives[:4]) + ".")

    confidence = 0.45
    confidence += min(0.25, 0.05 * len(facts))
    confidence += min(0.20, 0.04 * len(disclosures))
    payload = decision(
        "Fundamentals Analyst",
        symbol,
        recommendation,
        confidence,
        reasons,
        {"facts": facts, "recent_disclosures": disclosures[:5]},
        score_delta=score_delta,
        hard_veto=hard_veto,
        run_id=run_id,
    )
    if persist:
        log_decision(payload)
        with _connect() as conn:
            ensure_fundamentals_schema(conn)
            conn.execute(
                """
                INSERT INTO fundamental_agent_reports (
                    symbol, recommendation, confidence, report_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    recommendation,
                    payload["confidence"],
                    json.dumps(payload, sort_keys=True, default=str),
                    datetime.now(IST).replace(microsecond=0).isoformat(),
                ),
            )
            conn.commit()
    return payload


if __name__ == "__main__":
    import sys

    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    print(json.dumps(analyze({"symbol": symbol_arg, "direction": "BUY"}), indent=2))
