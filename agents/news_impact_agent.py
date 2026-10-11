"""News Impact Agent.

Separates trade-moving news from ordinary sentiment. This agent can boost,
reduce, block, or ignore a trade candidate.
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

BLOCK_KEYWORDS = {
    "fraud",
    "default",
    "bankruptcy",
    "investigation",
    "raid",
    "forensic audit",
    "sebi notice",
    "regulatory action",
    "credit rating downgrade",
    "promoter selling",
}
BOOST_KEYWORDS = {
    "record profit",
    "beats estimates",
    "buyback",
    "dividend",
    "new order",
    "order win",
    "credit rating upgrade",
    "promoter buying",
    "expansion",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def _load_recent_items(symbol: str, hours: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(IST) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    items: list[dict[str, Any]] = []
    with _connect() as conn:
        if _table_exists(conn, "news"):
            rows = conn.execute(
                """
                SELECT 'news' AS kind, headline, summary, source, url,
                       published_at, sentiment_score, urgency, NULL AS signal_boost
                FROM news
                WHERE (UPPER(symbol) = UPPER(?)
                       OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?)
                  AND COALESCE(published_at, fetched_at) >= ?
                ORDER BY COALESCE(published_at, fetched_at) DESC
                LIMIT 12
                """,
                (symbol, f"%{symbol.upper()}%", cutoff),
            ).fetchall()
            items.extend(dict(row) for row in rows)
        if _table_exists(conn, "announcements"):
            rows = conn.execute(
                """
                SELECT 'announcement' AS kind, headline, details AS summary,
                       exchange AS source, NULL AS url, published_at,
                       sentiment_score, 'HIGH' AS urgency, signal_boost
                FROM announcements
                WHERE UPPER(symbol) = UPPER(?)
                  AND COALESCE(published_at, fetched_at) >= ?
                ORDER BY COALESCE(published_at, fetched_at) DESC
                LIMIT 12
                """,
                (symbol, cutoff),
            ).fetchall()
            items.extend(dict(row) for row in rows)
    return items


def analyze(plan: dict[str, Any], run_id: str | None = None, persist: bool = False) -> dict[str, Any]:
    """Vote on whether recent news supports, blocks, or should be ignored."""
    symbol = str(plan.get("symbol", "")).upper()
    direction = str(plan.get("direction", "")).upper()
    items = _load_recent_items(symbol, hours=24)
    if not items:
        payload = decision(
            "News Impact Agent",
            symbol,
            "IGNORE",
            0.5,
            ["No recent stock-specific news or announcements found."],
            {"items_checked": 0},
            score_delta=0,
            run_id=run_id,
        )
        if persist:
            log_decision(payload)
        return payload

    combined_text = " ".join(
        f"{item.get('headline', '')} {item.get('summary', '')}".lower()
        for item in items
    )
    matched_blocks = sorted(keyword for keyword in BLOCK_KEYWORDS if keyword in combined_text)
    matched_boosts = sorted(keyword for keyword in BOOST_KEYWORDS if keyword in combined_text)
    sentiment_values = [_safe_float(item.get("sentiment_score")) for item in items]
    avg_sentiment = sum(sentiment_values) / len(sentiment_values) if sentiment_values else 0.0
    boost_total = sum(_safe_float(item.get("signal_boost")) for item in items)

    reasons: list[str] = []
    hard_veto = False
    score_delta = 0.0
    recommendation = "IGNORE"
    confidence = min(0.9, 0.45 + 0.05 * len(items))

    if matched_blocks and direction == "BUY":
        recommendation = "BLOCK_TRADE"
        hard_veto = True
        score_delta = -25
        reasons.append(f"High-risk negative news found: {', '.join(matched_blocks[:4])}.")
    elif matched_blocks:
        recommendation = "REDUCE_CONFIDENCE"
        score_delta = -10
        reasons.append(f"Negative news risk present: {', '.join(matched_blocks[:4])}.")
    elif matched_boosts and avg_sentiment >= -0.1:
        recommendation = "BOOST_TRADE"
        score_delta = min(12.0, 4.0 + abs(boost_total) / 4)
        reasons.append(f"Potentially trade-moving positive news: {', '.join(matched_boosts[:4])}.")
    elif avg_sentiment <= -0.35:
        recommendation = "REDUCE_CONFIDENCE"
        score_delta = -8
        reasons.append(f"Recent sentiment is negative ({avg_sentiment:+.2f}).")
    elif avg_sentiment >= 0.35:
        recommendation = "BOOST_TRADE"
        score_delta = 6
        reasons.append(f"Recent sentiment is positive ({avg_sentiment:+.2f}).")
    else:
        reasons.append(f"Recent news is not decisive ({avg_sentiment:+.2f} average sentiment).")

    payload = decision(
        "News Impact Agent",
        symbol,
        recommendation,
        confidence,
        reasons,
        {
            "items_checked": len(items),
            "avg_sentiment": round(avg_sentiment, 4),
            "signal_boost_total": round(boost_total, 2),
            "matched_blocks": matched_blocks,
            "matched_boosts": matched_boosts,
            "latest_items": [
                {
                    "kind": item.get("kind"),
                    "headline": item.get("headline"),
                    "source": item.get("source"),
                    "published_at": item.get("published_at"),
                }
                for item in items[:5]
            ],
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
