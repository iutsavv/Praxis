"""Corporate announcement fetcher for NSE/BSE exchange disclosures."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # Allows local DB boost reads even before dependencies are installed.
    requests = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
BSE_ANNOUNCEMENTS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.nseindia.com",
    "Accept": "application/json,text/plain,*/*",
}
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json,text/plain,*/*",
}

ANNOUNCEMENT_IMPACTS: dict[str, tuple[float, float]] = {
    "Financial Results": (+0.3, +15),
    "Results-Beat": (+0.8, +25),
    "Results-Miss": (-0.8, -25),
    "Dividend": (+0.6, +15),
    "Buyback": (+0.7, +20),
    "Board Meeting": (0.0, +5),
    "Merger-Acquisition": (+0.5, +20),
    "New Order": (+0.6, +15),
    "Expansion": (+0.4, +10),
    "Promoter-Buying": (+0.6, +15),
    "Promoter-Selling": (-0.5, -20),
    "Regulatory-Action": (-0.8, -30),
    "Investigation": (-0.9, -35),
    "Management-Change": (-0.3, -10),
    "Debt-Default": (-1.0, -40),
    "Credit-Rating-Upgrade": (+0.7, +20),
    "Credit-Rating-Downgrade": (-0.7, -25),
}

KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Debt-Default", ("default", "non payment", "non-payment", "delay in payment")),
    ("Investigation", ("investigation", "search", "raid", "forensic audit")),
    ("Regulatory-Action", ("penalty", "show cause", "sebi", "rbi", "enforcement", "regulatory action")),
    ("Credit-Rating-Downgrade", ("rating downgrade", "downgraded", "revision in rating downward")),
    ("Credit-Rating-Upgrade", ("rating upgrade", "upgraded", "revision in rating upward")),
    ("Promoter-Selling", ("promoter sell", "promoter selling", "disposal of shares", "pledge invocation")),
    ("Promoter-Buying", ("promoter buy", "promoter buying", "acquisition of shares")),
    ("Results-Beat", ("above estimate", "beat", "record profit", "highest ever", "strong results")),
    ("Results-Miss", ("below estimate", "miss", "loss widened", "weak results", "decline in profit")),
    ("Financial Results", ("financial results", "audited results", "unaudited results", "quarterly results")),
    ("Dividend", ("dividend", "interim dividend", "final dividend")),
    ("Buyback", ("buyback", "buy-back")),
    ("Merger-Acquisition", ("merger", "amalgamation", "acquisition", "scheme of arrangement")),
    ("New Order", ("order win", "new order", "letter of award", "contract awarded", "purchase order")),
    ("Expansion", ("expansion", "capacity addition", "new plant", "commercial production")),
    ("Management-Change", ("resignation", "appointment", "ceased to be", "chief executive", "chief financial")),
    ("Board Meeting", ("board meeting", "meeting of the board")),
]


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_announcements_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT NOT NULL,
            exchange            TEXT,
            announcement_type   TEXT,
            headline            TEXT,
            details             TEXT,
            published_at        TEXT,
            sentiment_score     REAL DEFAULT 0,
            impact_score        REAL DEFAULT 0,
            signal_boost        REAL DEFAULT 0,
            fetched_at          TEXT DEFAULT (datetime('now')),
            processed           INTEGER DEFAULT 0,
            UNIQUE(symbol, published_at, headline)
        )
        """
    )
    conn.commit()


def _stock_universe_symbols() -> list[str]:
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT symbol FROM stock_universe WHERE is_active = 1"
        ).fetchall()
    return [row["symbol"] for row in rows]


def _first_value(row: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return default


def _normalise_symbol(value: str) -> str:
    return value.strip().upper().replace(".NS", "").replace("NSE:", "")


def _normalise_published_at(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(value[: len(datetime.now().strftime(fmt))], fmt).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            continue
    return value


def _make_nse_session() -> requests.Session:
    if requests is None:
        raise RuntimeError("requests is not installed. Run pip install -r requirements.txt")
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    session.get("https://www.nseindia.com", timeout=15)
    return session


def fetch_nse_announcements(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Fetch latest NSE corporate announcements."""
    symbol_filter = {_normalise_symbol(symbol) for symbol in symbols} if symbols else None
    session = _make_nse_session()
    response = session.get(NSE_ANNOUNCEMENTS_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    rows = payload if isinstance(payload, list) else payload.get("data", [])

    announcements: list[dict[str, Any]] = []
    for item in rows[:50]:
        if not isinstance(item, dict):
            continue
        symbol = _normalise_symbol(_first_value(item, ("symbol", "sym", "companySymbol")))
        if not symbol or (symbol_filter and symbol not in symbol_filter):
            continue
        headline = _first_value(item, ("desc", "headline", "subject", "sm_desc", "an_desc"))
        details = _first_value(item, ("attchmntText", "details", "sm_name", "companyName"))
        published_at = _normalise_published_at(
            _first_value(item, ("an_dt", "sort_date", "published_at", "date"))
        )
        announcements.append(
            {
                "symbol": symbol,
                "exchange": "NSE",
                "headline": headline or details,
                "details": details,
                "published_at": published_at,
            }
        )
    return announcements


def fetch_bse_announcements(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Fetch latest BSE corporate announcements."""
    if requests is None:
        raise RuntimeError("requests is not installed. Run pip install -r requirements.txt")
    symbol_filter = {_normalise_symbol(symbol) for symbol in symbols} if symbols else None
    today = datetime.now().strftime("%Y%m%d")
    params = {
        "pageno": 1,
        "strCat": -1,
        "strPrevDate": today,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": today,
        "strType": "C",
        "subcategory": -1,
    }
    response = requests.get(BSE_ANNOUNCEMENTS_URL, headers=BSE_HEADERS, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("Table", payload if isinstance(payload, list) else [])

    announcements: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = _normalise_symbol(
            _first_value(item, ("SCRIP_SYMBOL", "SCRIP_NAME", "SCRIP_CD", "SCRIP_CODE", "NEWS_SUB"))
        )
        headline = _first_value(item, ("HEADLINE", "NEWS_HEAD", "SUBJECT", "SLONGNAME"))
        details = _first_value(item, ("MORE", "NEWS_BODY", "CATEGORYNAME", "SUBCATNAME"))
        if symbol_filter and symbol not in symbol_filter:
            # BSE often returns numeric scrip codes instead of NSE symbols.
            # Keep unfiltered runs broad; only strict-filter when a symbol matches.
            continue
        published_at = _normalise_published_at(
            _first_value(item, ("NEWS_DT", "DT_TM", "DissemDT", "DATE"))
        )
        if symbol and headline:
            announcements.append(
                {
                    "symbol": symbol,
                    "exchange": "BSE",
                    "headline": headline,
                    "details": details,
                    "published_at": published_at,
                }
            )
    return announcements


def classify_announcement(announcement: dict[str, Any]) -> dict[str, Any]:
    """Classify announcement type and assign sentiment/boost."""
    text = f"{announcement.get('headline', '')} {announcement.get('details', '')}".lower()
    announcement_type = "Board Meeting"
    for candidate_type, keywords in KEYWORD_RULES:
        if any(keyword in text for keyword in keywords):
            announcement_type = candidate_type
            break

    sentiment_score, signal_boost = ANNOUNCEMENT_IMPACTS.get(announcement_type, (0.0, 0.0))
    enriched = dict(announcement)
    enriched["announcement_type"] = announcement_type
    enriched["sentiment_score"] = sentiment_score
    enriched["signal_boost"] = signal_boost
    enriched["impact_score"] = abs(signal_boost)
    return enriched


def get_signal_boost_for_stock(symbol: str, hours: int = 24) -> float:
    """Return capped combined announcement boost for a symbol."""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with _get_connection() as conn:
        ensure_announcements_schema(conn)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(signal_boost), 0) AS boost
            FROM announcements
            WHERE UPPER(symbol) = UPPER(?)
              AND published_at >= ?
            """,
            (symbol, cutoff),
        ).fetchone()
    boost = float(row["boost"] or 0.0)
    return max(-40.0, min(30.0, boost))


def _store_announcements(announcements: list[dict[str, Any]]) -> tuple[int, int]:
    with _get_connection() as conn:
        ensure_announcements_schema(conn)
        cursor = conn.cursor()
        inserted = 0
        high_impact = 0
        seen: set[tuple[str, str, str]] = set()
        for announcement in announcements:
            enriched = classify_announcement(announcement)
            key = (
                enriched.get("symbol", ""),
                enriched.get("published_at", ""),
                enriched.get("headline", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            if abs(float(enriched.get("signal_boost", 0))) >= 20:
                high_impact += 1
            cursor.execute(
                """
                INSERT OR IGNORE INTO announcements (
                    symbol, exchange, announcement_type, headline, details,
                    published_at, sentiment_score, impact_score, signal_boost,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    enriched.get("symbol", ""),
                    enriched.get("exchange", ""),
                    enriched.get("announcement_type", ""),
                    enriched.get("headline", ""),
                    enriched.get("details", ""),
                    enriched.get("published_at", ""),
                    enriched.get("sentiment_score", 0.0),
                    enriched.get("impact_score", 0.0),
                    enriched.get("signal_boost", 0.0),
                ),
            )
            inserted += cursor.rowcount
        conn.commit()
    return inserted, high_impact


def run_announcement_fetch() -> dict[str, Any]:
    """Fetch, classify, dedupe, and store NSE/BSE corporate announcements."""
    symbols = _stock_universe_symbols()
    fetched: list[dict[str, Any]] = []
    errors: list[str] = []

    for fetcher_name, fetcher in (
        ("NSE", fetch_nse_announcements),
        ("BSE", fetch_bse_announcements),
    ):
        try:
            fetched.extend(fetcher(symbols))
        except Exception as exc:
            errors.append(f"{fetcher_name}: {exc}")
            time.sleep(1)

    new_announcements, high_impact_count = _store_announcements(fetched)
    return {
        "total_fetched": len(fetched),
        "new_announcements": new_announcements,
        "high_impact_count": high_impact_count,
        "errors": errors,
    }


if __name__ == "__main__":
    print(run_announcement_fetch())
