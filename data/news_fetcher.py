"""Free RSS news fetcher for market and stock news."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore[assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"
ENV_PATH = PROJECT_ROOT / ".env"
GOOGLE_NEWS_STAGE1_LIMIT = 50
NEWSAPI_STAGE2_LIMIT = 5
NEWSAPI_DAILY_LIMIT = 90
NEWSAPI_NAME = "newsapi"
NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSAPI_ALLOWED_SOURCES = (
    "reuters",
    "bloomberg",
    "economic times",
    "mint",
    "livemint",
    "business standard",
)

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    "economic_times_markets": "https://economictimes.indiatimes.com/markets/rss.cms",
    "economic_times_stocks": "https://economictimes.indiatimes.com/markets/stocks/rss.cms",
    "moneycontrol_news": "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "business_standard": "https://www.business-standard.com/rss/markets-106.rss",
    "livemint_markets": "https://www.livemint.com/rss/markets",
    "financial_express": "https://www.financialexpress.com/market/feed/",
}

POSITIVE_KEYWORDS = {
    "surge",
    "rises",
    "rally",
    "gain",
    "beats",
    "profit",
    "upgrade",
    "buyback",
    "dividend",
    "order win",
    "expansion",
}
NEGATIVE_KEYWORDS = {
    "fall",
    "falls",
    "drops",
    "loss",
    "miss",
    "downgrade",
    "default",
    "probe",
    "penalty",
    "fraud",
    "resigns",
}
HIGH_URGENCY_KEYWORDS = {
    "default",
    "fraud",
    "probe",
    "penalty",
    "crash",
    "surges",
    "buyback",
    "downgrade",
    "upgrade",
    "merger",
}


def google_news_url(symbol: str, company_name: str) -> str:
    query = f"{company_name} NSE stock"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_env_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    if not ENV_PATH.exists():
        return ""

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        env_key, env_value = stripped.split("=", 1)
        if env_key.strip() == key:
            return env_value.strip().strip('"').strip("'")
    return ""


def ensure_news_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT,
            headline        TEXT NOT NULL,
            summary         TEXT,
            source          TEXT,
            url             TEXT,
            published_at    TEXT,
            sentiment_score REAL DEFAULT 0,
            keywords_json   TEXT,
            urgency         TEXT DEFAULT 'LOW',
            stocks_mentioned TEXT,
            fetched_at      TEXT DEFAULT (datetime('now')),
            processed       INTEGER DEFAULT 0,
            UNIQUE(url)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(news)").fetchall()}
    if "processed" not in columns:
        conn.execute("ALTER TABLE news ADD COLUMN processed INTEGER DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            api_name    TEXT,
            call_date   TEXT,
            call_count  INTEGER DEFAULT 0,
            UNIQUE(api_name, call_date)
        )
        """
    )
    conn.commit()


def _load_stock_lookup() -> list[dict[str, str]]:
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, COALESCE(name, symbol) AS name
            FROM stock_universe
            WHERE is_active = 1
            UNION
            SELECT symbol, COALESCE(name, symbol) AS name
            FROM stocks
            WHERE is_active = 1
            """
        ).fetchall()
    return [{"symbol": row["symbol"].upper(), "name": row["name"]} for row in rows]


def _load_latest_stage1_flagged(limit: int = GOOGLE_NEWS_STAGE1_LIMIT) -> list[dict[str, str]]:
    """Return latest Stage 1 symbols for targeted Google News checks."""
    with _get_connection() as conn:
        latest = conn.execute(
            "SELECT MAX(scan_time) FROM scan_results WHERE stage1_flagged = 1"
        ).fetchone()[0]
        if not latest:
            return []

        rows = conn.execute(
            """
            SELECT sr.symbol, COALESCE(su.name, s.name, sr.symbol) AS name
            FROM scan_results sr
            LEFT JOIN stock_universe su ON su.symbol = sr.symbol
            LEFT JOIN stocks s ON s.symbol = sr.symbol
            WHERE sr.scan_time = ?
              AND sr.stage1_flagged = 1
            ORDER BY ABS(COALESCE(sr.stage1_price_change, 0)) DESC,
                     COALESCE(sr.stage1_volume_ratio, 0) DESC
            LIMIT ?
            """,
            (latest, limit),
        ).fetchall()

    return [{"symbol": row["symbol"].upper(), "name": row["name"]} for row in rows]


def _load_latest_stage2_candidates(limit: int = NEWSAPI_STAGE2_LIMIT) -> list[dict[str, str]]:
    """Return top Stage 2 candidates for paid/quota-limited news enrichment."""
    with _get_connection() as conn:
        latest = conn.execute(
            "SELECT MAX(scan_time) FROM scan_results WHERE stage = 2"
        ).fetchone()[0]
        if not latest:
            return []

        rows = conn.execute(
            """
            SELECT sr.symbol, COALESCE(su.name, s.name, sr.symbol) AS name
            FROM scan_results sr
            LEFT JOIN stock_universe su ON su.symbol = sr.symbol
            LEFT JOIN stocks s ON s.symbol = sr.symbol
            WHERE sr.scan_time = ?
              AND sr.stage = 2
            ORDER BY sr.final_selected DESC,
                     COALESCE(sr.stage2_score, 0) DESC
            LIMIT ?
            """,
            (latest, limit),
        ).fetchall()

    return [{"symbol": row["symbol"].upper(), "name": row["name"]} for row in rows]


def get_daily_api_usage() -> int:
    """Return today's successful NewsAPI call count."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_connection() as conn:
        ensure_news_schema(conn)
        row = conn.execute(
            "SELECT call_count FROM api_usage WHERE api_name = ? AND call_date = ?",
            (NEWSAPI_NAME, today),
        ).fetchone()
    return int(row["call_count"]) if row else 0


def _increment_api_usage(api_name: str = NEWSAPI_NAME) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    with _get_connection() as conn:
        ensure_news_schema(conn)
        conn.execute(
            """
            INSERT INTO api_usage (api_name, call_date, call_count)
            VALUES (?, ?, 1)
            ON CONFLICT(api_name, call_date)
            DO UPDATE SET call_count = call_count + 1
            """,
            (api_name, today),
        )
        conn.commit()


def _normalise_date(entry: Any) -> str:
    for attr in ("published", "updated", "created"):
        value = getattr(entry, attr, None) if not isinstance(entry, dict) else entry.get(attr)
        if value:
            try:
                return parsedate_to_datetime(str(value)).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, IndexError):
                return str(value)
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"<[^>]+>", " ", value).replace("&nbsp;", " ").strip()


def fetch_rss_feed(url: str, source_name: str) -> list[dict[str, Any]]:
    """Parse one RSS feed into normalized article dictionaries."""
    if feedparser is None:
        raise RuntimeError("feedparser is not installed. Run pip install -r requirements.txt")

    try:
        # Several publishers reject feedparser's default HTTP client or take
        # too long to respond. Fetch explicitly with a user agent and timeout.
        if requests is not None:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AITradingAgent/1.0)"},
                timeout=15,
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
        else:
            parsed = feedparser.parse(url)
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", source_name, exc)
        return []

    articles: list[dict[str, Any]] = []
    for entry in getattr(parsed, "entries", []):
        headline = _clean_text(getattr(entry, "title", ""))
        link = str(getattr(entry, "link", "")).strip()
        if not headline or not link:
            continue
        summary = _clean_text(getattr(entry, "summary", ""))
        articles.append(
            {
                "headline": headline,
                "summary": summary,
                "source": source_name,
                "url": link,
                "published_at": _normalise_date(entry),
            }
        )
    return articles


def fetch_all_rss_feeds() -> list[dict[str, Any]]:
    """Fetch all configured RSS feeds and dedupe by URL."""
    if feedparser is None:
        raise RuntimeError("feedparser is not installed. Run pip install -r requirements.txt")

    by_url: dict[str, dict[str, Any]] = {}
    for source_name, url in RSS_FEEDS.items():
        try:
            for article in fetch_rss_feed(url, source_name):
                by_url.setdefault(article["url"], article)
        except Exception:
            continue
    return list(by_url.values())


def fetch_google_news_for_symbol(symbol: str, company_name: str) -> list[dict[str, Any]]:
    """Fetch Google News RSS for a Stage 1 flagged stock."""
    return fetch_rss_feed(google_news_url(symbol, company_name), f"google_news_{symbol}")


def fetch_newsapi_for_symbol(symbol: str, company_name: str) -> list[dict[str, Any]]:
    """Fetch high-quality NewsAPI articles for one Stage 2 candidate."""
    usage = get_daily_api_usage()
    if usage >= NEWSAPI_DAILY_LIMIT:
        logger.warning("NewsAPI daily limit guard reached: %s/%s calls used", usage, NEWSAPI_DAILY_LIMIT)
        return []
    if usage >= NEWSAPI_DAILY_LIMIT - 10:
        logger.warning("NewsAPI usage approaching guard limit: %s/%s calls used", usage, NEWSAPI_DAILY_LIMIT)

    api_key = _load_env_value("NEWS_API_KEY")
    if not api_key or api_key == "your_free_key_from_newsapi_org":
        logger.warning("NEWS_API_KEY is not configured; skipping NewsAPI fetch")
        return []

    if requests is None:
        raise RuntimeError("requests is not installed. Run pip install -r requirements.txt")

    params = {
        "q": f"{company_name} NSE",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 20,
        "apiKey": api_key,
    }
    response = requests.get(NEWSAPI_URL, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") and payload.get("status") != "ok":
        raise RuntimeError(f"NewsAPI returned {payload.get('status')}: {payload.get('message', 'unknown error')}")
    _increment_api_usage()

    articles: list[dict[str, Any]] = []
    for item in payload.get("articles", []):
        source_name = str((item.get("source") or {}).get("name") or "").strip()
        source_key = source_name.lower()
        if not source_key or not any(allowed in source_key for allowed in NEWSAPI_ALLOWED_SOURCES):
            continue

        headline = _clean_text(item.get("title"))
        url = str(item.get("url") or "").strip()
        if not headline or not url:
            continue

        articles.append(
            {
                "headline": headline,
                "summary": _clean_text(item.get("description") or item.get("content")),
                "source": f"newsapi:{source_name or 'unknown'}",
                "url": url,
                "published_at": str(item.get("publishedAt") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "symbol": symbol.upper(),
            }
        )
    return articles


def extract_mentioned_stocks(text: str, all_symbols: list[Any]) -> list[str]:
    """Return NSE symbols mentioned in article text by symbol or company name."""
    haystack = f" {text.lower()} "
    mentioned: list[str] = []
    seen: set[str] = set()

    for item in all_symbols:
        if isinstance(item, dict):
            symbol = str(item.get("symbol", "")).upper()
            name = str(item.get("name", ""))
        else:
            symbol = str(item).upper()
            name = ""
        if not symbol or symbol in seen:
            continue

        symbol_match = re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text.upper())
        name_match = bool(name and len(name) >= 4 and name.lower() in haystack)
        if symbol_match or name_match:
            mentioned.append(symbol)
            seen.add(symbol)
    return mentioned


def _score_article(text: str) -> tuple[float, list[str], str]:
    lowered = text.lower()
    matched_positive = sorted(keyword for keyword in POSITIVE_KEYWORDS if keyword in lowered)
    matched_negative = sorted(keyword for keyword in NEGATIVE_KEYWORDS if keyword in lowered)
    high_urgency = sorted(keyword for keyword in HIGH_URGENCY_KEYWORDS if keyword in lowered)

    sentiment = 0.0
    if matched_positive:
        sentiment += min(0.8, 0.2 * len(matched_positive))
    if matched_negative:
        sentiment -= min(0.9, 0.25 * len(matched_negative))
    sentiment = max(-1.0, min(1.0, round(sentiment, 2)))

    urgency = "HIGH" if high_urgency else "MEDIUM" if matched_positive or matched_negative else "LOW"
    keywords = sorted(set(matched_positive + matched_negative + high_urgency))
    return sentiment, keywords, urgency


def _store_articles(articles: list[dict[str, Any]], stock_lookup: list[dict[str, str]]) -> tuple[int, set[str]]:
    with _get_connection() as conn:
        ensure_news_schema(conn)
        cursor = conn.cursor()
        inserted = 0
        mentioned_symbols: set[str] = set()

        for article in articles:
            text = f"{article.get('headline', '')} {article.get('summary', '')}"
            mentions = extract_mentioned_stocks(text, stock_lookup)
            article_symbol = str(article.get("symbol") or "").upper()
            if article_symbol and article_symbol not in mentions:
                mentions.insert(0, article_symbol)
            mentioned_symbols.update(mentions)
            sentiment, keywords, urgency = _score_article(text)
            cursor.execute(
                """
                INSERT OR IGNORE INTO news (
                    symbol, headline, summary, source, url, published_at,
                    sentiment_score, keywords_json, urgency, stocks_mentioned,
                    processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    mentions[0] if mentions else None,
                    article.get("headline", ""),
                    article.get("summary", ""),
                    article.get("source", ""),
                    article.get("url", ""),
                    article.get("published_at", ""),
                    sentiment,
                    json.dumps(keywords),
                    urgency,
                    json.dumps(mentions),
                ),
            )
            inserted += cursor.rowcount

        conn.commit()
    return inserted, mentioned_symbols


def run_news_fetch() -> dict[str, Any]:
    """Fetch free RSS market news, link to stocks, and store new articles."""
    stock_lookup = _load_stock_lookup()
    by_url = {article["url"]: article for article in fetch_all_rss_feeds()}
    stage1_symbols = _load_latest_stage1_flagged()
    stage2_symbols = _load_latest_stage2_candidates()

    for stock in stage1_symbols:
        try:
            for article in fetch_google_news_for_symbol(stock["symbol"], stock["name"]):
                by_url.setdefault(article["url"], article)
        except Exception:
            continue

    newsapi_calls_before = get_daily_api_usage()
    for stock in stage2_symbols:
        if get_daily_api_usage() >= NEWSAPI_DAILY_LIMIT:
            logger.warning("Skipping remaining NewsAPI calls; daily guard limit reached")
            break
        try:
            for article in fetch_newsapi_for_symbol(stock["symbol"], stock["name"]):
                by_url.setdefault(article["url"], article)
        except Exception as exc:
            logger.warning("NewsAPI fetch failed for %s: %s", stock["symbol"], exc)
            continue

    articles = list(by_url.values())
    new_articles, stocks_mentioned = _store_articles(articles, stock_lookup)
    newsapi_calls_after = get_daily_api_usage()
    return {
        "total_articles": len(articles),
        "new_articles": new_articles,
        "stocks_mentioned": len(stocks_mentioned),
        "google_symbols_checked": len(stage1_symbols),
        "newsapi_symbols_checked": len(stage2_symbols),
        "newsapi_calls_used": newsapi_calls_after - newsapi_calls_before,
        "newsapi_daily_usage": newsapi_calls_after,
    }


if __name__ == "__main__":
    print(run_news_fetch())
