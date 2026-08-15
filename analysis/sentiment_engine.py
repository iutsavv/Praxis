"""Local finance sentiment engine for NSE news and announcements."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"
URGENCY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
URGENCY_WEIGHT = {"LOW": 1.0, "MEDIUM": 1.25, "HIGH": 1.6}

logger = logging.getLogger(__name__)

_VADER_ANALYZER: Any | None = None
_SPACY_NLP: Any | None = None
_SPACY_LOAD_ATTEMPTED = False


def build_finance_keyword_dict() -> dict[str, tuple[float, str]]:
    """Return Indian market keyword rules as sentiment modifier + urgency."""
    return {
        # Strongly positive
        "record profit": (+0.9, "HIGH"),
        "beats estimates": (+0.8, "HIGH"),
        "dividend declared": (+0.7, "HIGH"),
        "buyback": (+0.8, "HIGH"),
        "all time high": (+0.7, "MEDIUM"),
        "new order": (+0.6, "MEDIUM"),
        "acquisition": (+0.5, "HIGH"),
        "expansion": (+0.5, "MEDIUM"),
        "promoter buying": (+0.7, "HIGH"),
        "credit rating upgrade": (+0.8, "HIGH"),
        "ipo listing gains": (+0.6, "HIGH"),
        "debt free": (+0.6, "MEDIUM"),
        "market share gain": (+0.5, "MEDIUM"),
        # Strongly negative
        "fraud": (-1.0, "HIGH"),
        "scam": (-1.0, "HIGH"),
        "investigation": (-0.9, "HIGH"),
        "default": (-1.0, "HIGH"),
        "bankruptcy": (-1.0, "HIGH"),
        "promoter pledge": (-0.7, "HIGH"),
        "promoter selling": (-0.6, "HIGH"),
        "miss estimates": (-0.8, "HIGH"),
        "regulatory action": (-0.9, "HIGH"),
        "sebi notice": (-0.9, "HIGH"),
        "npa": (-0.7, "HIGH"),
        "bad loans": (-0.7, "MEDIUM"),
        "loss widened": (-0.8, "HIGH"),
        "management resignation": (-0.6, "HIGH"),
        "credit rating downgrade": (-0.8, "HIGH"),
        "debt restructuring": (-0.7, "HIGH"),
        # Moderately positive
        "profit": (+0.4, "LOW"),
        "growth": (+0.3, "LOW"),
        "upgrade": (+0.5, "MEDIUM"),
        "outperform": (+0.4, "MEDIUM"),
        "strong demand": (+0.3, "LOW"),
        "margin expansion": (+0.5, "MEDIUM"),
        # Moderately negative
        "loss": (-0.4, "LOW"),
        "decline": (-0.3, "LOW"),
        "downgrade": (-0.5, "MEDIUM"),
        "weak demand": (-0.3, "LOW"),
        "margin pressure": (-0.4, "MEDIUM"),
    }


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_sentiment_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(news)").fetchall()}
    if "processed" not in columns:
        conn.execute("ALTER TABLE news ADD COLUMN processed INTEGER DEFAULT 0")
    conn.commit()


def _get_vader_score(text: str) -> float:
    global _VADER_ANALYZER
    if _VADER_ANALYZER is None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            _VADER_ANALYZER = SentimentIntensityAnalyzer()
        except Exception as exc:  # pragma: no cover - environment dependent.
            logger.warning("VADER unavailable, using neutral fallback: %s", exc)
            return 0.0
    return float(_VADER_ANALYZER.polarity_scores(text or "")["compound"])


def _load_spacy() -> Any | None:
    global _SPACY_LOAD_ATTEMPTED, _SPACY_NLP
    if _SPACY_LOAD_ATTEMPTED:
        return _SPACY_NLP
    _SPACY_LOAD_ATTEMPTED = True
    try:
        if hasattr(os, "add_dll_directory"):
            try:
                import numpy

                numpy_libs = Path(numpy.__file__).resolve().parents[1] / "numpy.libs"
                if numpy_libs.exists():
                    os.add_dll_directory(str(numpy_libs))
            except Exception:
                pass
        import spacy

        _SPACY_NLP = spacy.load("en_core_web_sm")
    except Exception as exc:  # pragma: no cover - environment dependent.
        logger.warning("spaCy NER unavailable; falling back to direct stock lookup: %s", exc)
        _SPACY_NLP = None
    return _SPACY_NLP


def _keyword_matches(text: str) -> list[tuple[str, float, str]]:
    lowered = (text or "").lower()
    rules = build_finance_keyword_dict()
    matches: list[tuple[str, float, str]] = []
    for keyword, (score, urgency) in rules.items():
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        if re.search(pattern, lowered):
            matches.append((keyword, score, urgency))
    return matches


def _label(score: float) -> str:
    if score >= 0.6:
        return "VERY_POSITIVE"
    if score >= 0.2:
        return "POSITIVE"
    if score <= -0.6:
        return "VERY_NEGATIVE"
    if score <= -0.2:
        return "NEGATIVE"
    return "NEUTRAL"


def analyze_sentiment(text: str) -> dict[str, Any]:
    """Combine VADER with Indian finance keyword rules."""
    clean_text = re.sub(r"\s+", " ", text or "").strip()
    vader_score = _get_vader_score(clean_text)
    matches = _keyword_matches(clean_text)

    if matches:
        keyword_score = sum(score for _, score, _ in matches) / len(matches)
        urgency = max((urgency for _, _, urgency in matches), key=lambda item: URGENCY_RANK[item])
        score = (vader_score * 0.4) + (keyword_score * 0.6)
    else:
        keyword_score = 0.0
        urgency = "LOW"
        score = vader_score

    score = round(_clamp(score), 4)
    confidence = 0.45
    confidence += min(0.3, 0.08 * len(matches))
    confidence += min(0.15, abs(score) * 0.15)
    confidence += 0.1 if len(clean_text.split()) >= 8 else 0.0
    if not matches:
        confidence -= 0.1

    return {
        "score": score,
        "label": _label(score),
        "urgency": urgency,
        "keywords_found": [keyword for keyword, _, _ in matches],
        "confidence": round(_clamp(confidence, 0.0, 1.0), 4),
    }


def _load_stock_universe(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT symbol, COALESCE(name, symbol) AS name
        FROM stock_universe
        WHERE is_active = 1
        """
    ).fetchall()
    return [{"symbol": row["symbol"].upper(), "name": row["name"] or row["symbol"]} for row in rows]


def extract_stock_entities(text: str) -> list[str]:
    """Extract NSE symbols mentioned in text using spaCy ORG names plus DB matching."""
    clean_text = re.sub(r"\s+", " ", text or "").strip()
    if not clean_text:
        return []

    with _get_connection() as conn:
        stocks = _load_stock_universe(conn)

    candidates = {clean_text}
    nlp = _load_spacy()
    if nlp is not None:
        doc = nlp(clean_text[:100_000])
        candidates.update(ent.text for ent in doc.ents if ent.label_ in {"ORG", "PRODUCT"})

    haystack = f" {clean_text.lower()} "
    found: list[str] = []
    seen: set[str] = set()
    for stock in stocks:
        symbol = stock["symbol"]
        name = stock["name"].strip()
        symbol_match = re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", clean_text.upper())
        name_match = bool(name and len(name) >= 4 and name.lower() in haystack)
        entity_match = any(name and len(name) >= 4 and name.lower() in candidate.lower() for candidate in candidates)
        if (symbol_match or name_match or entity_match) and symbol not in seen:
            found.append(symbol)
            seen.add(symbol)
    return found


def process_unprocessed_news() -> int:
    """Analyze news rows where processed=0 and mark them processed."""
    with _get_connection() as conn:
        _ensure_sentiment_schema(conn)
        rows = conn.execute(
            """
            SELECT id, headline, summary
            FROM news
            WHERE COALESCE(processed, 0) = 0
            ORDER BY COALESCE(published_at, fetched_at) ASC, id ASC
            """
        ).fetchall()

        processed = 0
        for row in rows:
            text = f"{row['headline'] or ''} {row['summary'] or ''}".strip()
            sentiment = analyze_sentiment(text)
            symbols = extract_stock_entities(text)
            conn.execute(
                """
                UPDATE news
                SET sentiment_score = ?,
                    urgency = ?,
                    keywords_json = ?,
                    stocks_mentioned = CASE
                        WHEN ? != '[]' THEN ?
                        ELSE stocks_mentioned
                    END,
                    processed = 1
                WHERE id = ?
                """,
                (
                    sentiment["score"],
                    sentiment["urgency"],
                    json.dumps(sentiment["keywords_found"]),
                    json.dumps(symbols),
                    json.dumps(symbols),
                    row["id"],
                ),
            )
            processed += 1
        conn.commit()
    return processed


def _parse_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now()
    text = text.replace("T", " ").replace("Z", "")
    if "+" in text:
        text = text.split("+", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now()


def _recency_weight(published_at: Any, hours: int) -> float:
    age_hours = max(0.0, (datetime.now() - _parse_datetime(published_at)).total_seconds() / 3600)
    return max(0.2, 1.0 - (age_hours / max(1, hours)))


def _announcement_urgency(row: sqlite3.Row) -> str:
    impact = abs(float(row["signal_boost"] or row["impact_score"] or 0.0))
    if impact >= 20:
        return "HIGH"
    if impact >= 10:
        return "MEDIUM"
    return "LOW"


def get_stock_news_sentiment(symbol: str, hours: int = 24) -> dict[str, Any]:
    """Return recent weighted sentiment for one NSE symbol."""
    normalized_symbol = symbol.upper().strip()
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with _get_connection() as conn:
        _ensure_sentiment_schema(conn)
        news_rows = conn.execute(
            """
            SELECT headline, sentiment_score, urgency, published_at, fetched_at
            FROM news
            WHERE (UPPER(symbol) = ?
                   OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?)
              AND COALESCE(published_at, fetched_at) >= ?
            """,
            (normalized_symbol, f"%{normalized_symbol}%", cutoff),
        ).fetchall()
        announcement_rows = conn.execute(
            """
            SELECT headline, sentiment_score, impact_score, signal_boost, published_at, fetched_at
            FROM announcements
            WHERE UPPER(symbol) = ?
              AND COALESCE(published_at, fetched_at) >= ?
            """,
            (normalized_symbol, cutoff),
        ).fetchall()

    weighted_total = 0.0
    total_weight = 0.0
    highest_headline = ""
    highest_impact = -1.0
    max_urgency = "LOW"

    for row in news_rows:
        urgency = row["urgency"] or "LOW"
        weight = URGENCY_WEIGHT.get(urgency, 1.0) * _recency_weight(row["published_at"] or row["fetched_at"], hours)
        score = float(row["sentiment_score"] or 0.0)
        weighted_total += score * weight
        total_weight += weight
        impact = abs(score) * weight
        if impact > highest_impact:
            highest_impact = impact
            highest_headline = row["headline"] or ""
        if URGENCY_RANK.get(urgency, 1) > URGENCY_RANK[max_urgency]:
            max_urgency = urgency

    for row in announcement_rows:
        urgency = _announcement_urgency(row)
        weight = URGENCY_WEIGHT[urgency] * _recency_weight(row["published_at"] or row["fetched_at"], hours)
        score = float(row["sentiment_score"] or 0.0)
        weighted_total += score * weight
        total_weight += weight
        impact = abs(score) * weight + abs(float(row["signal_boost"] or 0.0)) / 40.0
        if impact > highest_impact:
            highest_impact = impact
            highest_headline = row["headline"] or ""
        if URGENCY_RANK[urgency] > URGENCY_RANK[max_urgency]:
            max_urgency = urgency

    overall = round(_clamp(weighted_total / total_weight), 4) if total_weight else 0.0
    return {
        "symbol": normalized_symbol,
        "overall_sentiment": overall,
        "article_count": len(news_rows),
        "announcement_count": len(announcement_rows),
        "highest_impact": highest_headline,
        "signal_modifier": round(max(-20.0, min(20.0, overall * 15.0)), 2),
        "urgency": max_urgency,
    }


if __name__ == "__main__":
    print({"processed": process_unprocessed_news()})
