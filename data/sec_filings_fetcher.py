"""SEC EDGAR fundamentals fetcher.

Uses public SEC JSON APIs to store filing metadata and XBRL company facts for
US-listed symbols. This complements the NSE announcement/news path; most NSE
symbols will not have SEC filings unless they also trade or report in the US.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = timezone(timedelta(hours=5, minutes=30))
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

METRIC_TAGS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "debt": ("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtAndFinanceLeaseObligationsNoncurrent", "LongTermDebtCurrent", "LongTermDebtNoncurrent"),
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_text() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


def _user_agent() -> str:
    return os.getenv("SEC_USER_AGENT", "AITradingAgent/1.0 contact@example.com")


def _session() -> requests.Session:
    if requests is None:
        raise RuntimeError("requests is not installed. Run pip install -r requirements.txt")
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _user_agent(),
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }
    )
    return session


def ensure_sec_schema(conn: sqlite3.Connection | None = None) -> None:
    from agents.fundamentals_analyst import ensure_fundamentals_schema

    owns_connection = conn is None
    if conn is None:
        conn = _connect()
    try:
        ensure_fundamentals_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_company_map (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL UNIQUE,
                cik             TEXT NOT NULL,
                title           TEXT,
                last_updated    TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _normalise_cik(cik: str | int) -> str:
    return str(cik).strip().zfill(10)


def fetch_company_tickers() -> dict[str, dict[str, Any]]:
    """Fetch SEC ticker to CIK mapping."""
    if requests is None:
        raise RuntimeError("requests is not installed. Run pip install -r requirements.txt")
    response = requests.get(
        SEC_TICKERS_URL,
        headers={"User-Agent": _user_agent()},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return {
        str(item.get("ticker", "")).upper(): {
            "ticker": str(item.get("ticker", "")).upper(),
            "cik": _normalise_cik(item.get("cik_str", "")),
            "title": item.get("title", ""),
        }
        for item in payload.values()
        if item.get("ticker") and item.get("cik_str")
    }


def resolve_ticker(ticker: str) -> dict[str, Any]:
    """Resolve a US ticker to SEC CIK metadata and cache it."""
    normalized = ticker.upper().strip()
    with _connect() as conn:
        ensure_sec_schema(conn)
        row = conn.execute(
            "SELECT ticker, cik, title FROM sec_company_map WHERE ticker = ?",
            (normalized,),
        ).fetchone()
        if row:
            return dict(row)

    mapping = fetch_company_tickers()
    if normalized not in mapping:
        raise ValueError(f"No SEC CIK found for ticker {normalized}")
    item = mapping[normalized]
    with _connect() as conn:
        ensure_sec_schema(conn)
        conn.execute(
            """
            INSERT INTO sec_company_map (ticker, cik, title, last_updated)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                cik = excluded.cik,
                title = excluded.title,
                last_updated = excluded.last_updated
            """,
            (item["ticker"], item["cik"], item["title"], _now_text()),
        )
        conn.commit()
    return item


def fetch_submissions(cik: str) -> dict[str, Any]:
    session = _session()
    response = session.get(SEC_SUBMISSIONS_URL.format(cik=_normalise_cik(cik)), timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_companyfacts(cik: str) -> dict[str, Any]:
    session = _session()
    response = session.get(SEC_COMPANYFACTS_URL.format(cik=_normalise_cik(cik)), timeout=45)
    response.raise_for_status()
    return response.json()


def _recent_filings(submissions: dict[str, Any], forms: set[str], limit: int) -> list[dict[str, Any]]:
    recent = submissions.get("filings", {}).get("recent", {})
    accession_numbers = recent.get("accessionNumber", [])
    result: list[dict[str, Any]] = []
    for index, accession in enumerate(accession_numbers):
        form = str(recent.get("form", [""])[index])
        if form not in forms:
            continue
        result.append(
            {
                "accession": accession,
                "form": form,
                "filing_date": recent.get("filingDate", [""])[index],
                "report_date": recent.get("reportDate", [""])[index],
                "primary_document": recent.get("primaryDocument", [""])[index],
                "description": recent.get("primaryDocDescription", [""])[index],
            }
        )
        if len(result) >= limit:
            break
    return result


def _store_filings(symbol: str, cik: str, title: str, submissions: dict[str, Any]) -> int:
    filings = _recent_filings(submissions, {"10-K", "10-Q", "8-K", "20-F", "6-K"}, 20)
    inserted = 0
    cik_int = str(int(cik))
    with _connect() as conn:
        ensure_sec_schema(conn)
        for filing in filings:
            accession_path = str(filing["accession"]).replace("-", "")
            primary_doc = filing.get("primary_document") or ""
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                f"{accession_path}/{primary_doc}"
                if primary_doc else None
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO fundamental_documents (
                    symbol, source, document_type, title, url, filing_date,
                    period_end, raw_text, parsed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    symbol.upper(),
                    "SEC EDGAR",
                    filing["form"],
                    filing.get("description") or f"{title} {filing['form']}",
                    url,
                    filing.get("filing_date"),
                    filing.get("report_date"),
                    _now_text(),
                ),
            )
            inserted += cursor.rowcount
        conn.commit()
    return inserted


def _unit_facts(companyfacts: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    fact = companyfacts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not fact:
        fact = companyfacts.get("facts", {}).get("ifrs-full", {}).get(tag)
    units = fact.get("units", {}) if isinstance(fact, dict) else {}
    rows: list[dict[str, Any]] = []
    for unit, facts in units.items():
        for item in facts:
            if item.get("val") is None or not item.get("end"):
                continue
            rows.append(
                {
                    "period_end": item.get("end"),
                    "fiscal_period": item.get("fp") or item.get("fy"),
                    "form": item.get("form"),
                    "value": item.get("val"),
                    "unit": unit,
                    "filed": item.get("filed"),
                }
            )
    rows.sort(key=lambda row: (str(row.get("period_end")), str(row.get("filed"))), reverse=True)
    return rows


def _latest_metric_rows(companyfacts: dict[str, Any], metric: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tag in METRIC_TAGS[metric]:
        for item in _unit_facts(companyfacts, tag):
            rows.append({**item, "tag": tag, "metric": metric})
    rows.sort(key=lambda row: (str(row.get("period_end")), str(row.get("filed"))), reverse=True)
    return rows[:8]


def _parse_date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _comparable_previous(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return latest and previous comparable facts for growth calculations."""
    if len(rows) < 2:
        return None
    latest = rows[0]
    latest_fp = str(latest.get("fiscal_period") or "")
    latest_end = _parse_date(latest.get("period_end"))
    latest_form = str(latest.get("form") or "")
    latest_unit = str(latest.get("unit") or "")

    candidates = []
    for row in rows[1:]:
        if latest_fp and str(row.get("fiscal_period") or "") != latest_fp:
            continue
        if latest_form and str(row.get("form") or "") != latest_form:
            continue
        if latest_unit and str(row.get("unit") or "") != latest_unit:
            continue
        row_end = _parse_date(row.get("period_end"))
        if latest_end and row_end and row_end >= latest_end:
            continue
        candidates.append(row)

    if not candidates:
        candidates = [
            row for row in rows[1:]
            if str(row.get("unit") or "") == latest_unit
        ]
    return (latest, candidates[0]) if candidates else None


def _store_fact(conn: sqlite3.Connection, symbol: str, period_end: str, fiscal_period: str, metric: str, value: float, unit: str) -> None:
    conn.execute(
        """
        INSERT INTO fundamental_facts (
            symbol, period_end, fiscal_period, metric, value, unit,
            source_document_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(symbol, period_end, fiscal_period, metric) DO UPDATE SET
            value = excluded.value,
            unit = excluded.unit,
            created_at = excluded.created_at
        """,
        (symbol.upper(), period_end, fiscal_period, metric, value, unit, _now_text()),
    )


def _store_companyfacts(symbol: str, companyfacts: dict[str, Any]) -> int:
    inserted = 0
    latest_by_metric: dict[str, list[dict[str, Any]]] = {}
    with _connect() as conn:
        ensure_sec_schema(conn)
        for metric in METRIC_TAGS:
            rows = _latest_metric_rows(companyfacts, metric)
            latest_by_metric[metric] = rows
            for row in rows:
                _store_fact(
                    conn,
                    symbol,
                    str(row["period_end"]),
                    str(row.get("fiscal_period") or ""),
                    metric,
                    float(row["value"]),
                    str(row["unit"]),
                )
                inserted += 1

        for base_metric, growth_metric in (
            ("revenue", "revenue_growth_pct"),
            ("net_income", "profit_growth_pct"),
        ):
            rows = latest_by_metric.get(base_metric, [])
            comparable = _comparable_previous(rows)
            if comparable:
                latest_row, previous_row = comparable
                latest = float(latest_row["value"])
                previous = float(previous_row["value"])
                if previous:
                    growth = (latest - previous) / abs(previous) * 100
                    _store_fact(
                        conn,
                        symbol,
                        str(latest_row["period_end"]),
                        str(latest_row.get("fiscal_period") or ""),
                        growth_metric,
                        growth,
                        "percent",
                    )
                    inserted += 1

        debt_rows = latest_by_metric.get("debt", [])
        equity_rows = latest_by_metric.get("equity", [])
        if debt_rows and equity_rows and float(equity_rows[0]["value"]):
            debt_to_equity = float(debt_rows[0]["value"]) / float(equity_rows[0]["value"])
            _store_fact(
                conn,
                symbol,
                str(debt_rows[0]["period_end"]),
                str(debt_rows[0].get("fiscal_period") or ""),
                "debt_to_equity",
                debt_to_equity,
                "ratio",
            )
            inserted += 1

        conn.commit()
    return inserted


def refresh_sec_fundamentals(ticker: str) -> dict[str, Any]:
    """Fetch and store SEC filings/facts for one US-listed ticker."""
    company = resolve_ticker(ticker)
    submissions = fetch_submissions(company["cik"])
    facts = fetch_companyfacts(company["cik"])
    documents = _store_filings(company["ticker"], company["cik"], company.get("title", ""), submissions)
    fact_count = _store_companyfacts(company["ticker"], facts)
    return {
        "symbol": company["ticker"],
        "cik": company["cik"],
        "title": company.get("title", ""),
        "documents_stored": documents,
        "facts_stored": fact_count,
    }


if __name__ == "__main__":
    ticker_arg = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(json.dumps(refresh_sec_fundamentals(ticker_arg), indent=2))
