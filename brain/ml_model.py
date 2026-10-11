"""Local ML model for paper-trade outcome prediction.

This module intentionally uses only local data and local model files. It does
not call external AI services.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

joblib = None
xgb = None
SimpleImputer = None
accuracy_score = None
StratifiedKFold = None
cross_val_score = None
train_test_split = None
Pipeline = None
StandardScaler = None
ML_DEPENDENCY_ERROR: str | None = None
ML_DEPENDENCIES_LOADED = False


def _load_ml_dependencies() -> bool:
    """Load heavy ML dependencies lazily so orchestration can use fallback rules."""
    global joblib, xgb, SimpleImputer, accuracy_score, StratifiedKFold
    global cross_val_score, train_test_split, Pipeline, StandardScaler
    global ML_DEPENDENCY_ERROR, ML_DEPENDENCIES_LOADED

    if ML_DEPENDENCIES_LOADED:
        return ML_DEPENDENCY_ERROR is None

    ML_DEPENDENCIES_LOADED = True
    try:
        import joblib as loaded_joblib
        import xgboost as loaded_xgb
        from sklearn.impute import SimpleImputer as LoadedSimpleImputer
        from sklearn.metrics import accuracy_score as loaded_accuracy_score
        from sklearn.model_selection import (
            StratifiedKFold as LoadedStratifiedKFold,
            cross_val_score as loaded_cross_val_score,
            train_test_split as loaded_train_test_split,
        )
        from sklearn.pipeline import Pipeline as LoadedPipeline
        from sklearn.preprocessing import StandardScaler as LoadedStandardScaler
    except Exception as exc:  # pragma: no cover - environment dependent.
        ML_DEPENDENCY_ERROR = str(exc)
        return False

    joblib = loaded_joblib
    xgb = loaded_xgb
    SimpleImputer = LoadedSimpleImputer
    accuracy_score = loaded_accuracy_score
    StratifiedKFold = LoadedStratifiedKFold
    cross_val_score = loaded_cross_val_score
    train_test_split = loaded_train_test_split
    Pipeline = LoadedPipeline
    StandardScaler = LoadedStandardScaler
    ML_DEPENDENCY_ERROR = None
    return True


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
MODELS_DIR = PROJECT_ROOT / "models"
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
MIN_TRAINING_TRADES = 50
TRADE_THRESHOLD = 0.55
log = logging.getLogger(__name__)


FEATURE_NAMES = [
    "rsi",
    "macd_hist",
    "ema20_vs_ema50_pct",
    "price_vs_vwap_pct",
    "volume_ratio",
    "rsi_momentum_3bar",
    "volume_acceleration",
    "price_vs_ema20_pct",
    "best_pattern_strength",
    "pattern_direction",
    "chart_pattern_detected",
    "distance_to_support_pct",
    "distance_to_resistance_pct",
    "at_support",
    "at_resistance",
    "support_strength",
    "resistance_strength",
    "setup_win_rate",
    "setup_match_quality",
    "setup_type_encoded",
    "news_sentiment",
    "has_announcement_today",
    "announcement_impact",
    "hour",
    "minute",
    "day_of_week",
    "minutes_since_open",
    "minutes_to_close",
    "market_condition_encoded",
    "nifty_momentum",
    "sector_momentum",
    "timeframe_confluence_score",
    "current_open_trades",
    "today_win_rate",
    "recent_win_rate",
    "daily_pnl_pct",
]

SETUP_TYPE_ENCODING = {
    "reversal": 0,
    "momentum": 1,
    "breakout": 2,
    "breakdown": 2,
    "continuation": 3,
    "mean_reversion": 4,
    "trend_following": 5,
    "catalyst": 6,
}
MARKET_CONDITION_ENCODING = {"SIDEWAYS": 0, "TRENDING": 1, "VOLATILE": 2}
PATTERN_DIRECTION_ENCODING = {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_ml_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_features (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id            INTEGER,
            symbol              TEXT,
            extracted_at        TEXT,
            features_json       TEXT,
            outcome             INTEGER,
            win_probability     REAL,
            model_version       INTEGER,
            FOREIGN KEY(trade_id) REFERENCES paper_trades(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_models (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            version                 INTEGER UNIQUE,
            trained_at              TEXT,
            training_trades         INTEGER,
            cv_accuracy             REAL,
            test_accuracy           REAL,
            feature_importance_json TEXT,
            model_file_path         TEXT,
            is_active               INTEGER DEFAULT 0,
            shadow_mode             INTEGER DEFAULT 1,
            notes                   TEXT
        )
        """
    )
    conn.commit()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _safe_pct(numerator: Any, denominator: Any) -> float:
    denom = _safe_float(denominator)
    if denom == 0:
        return 0.0
    return (_safe_float(numerator) - denom) / denom * 100


def _now_ist() -> datetime:
    return datetime.now(IST)


def _latest_indicator_rows(conn: sqlite3.Connection, symbol: str, limit: int = 4) -> list[sqlite3.Row]:
    if not _table_exists(conn, "indicators"):
        return []
    return conn.execute(
        """
        SELECT i.*, c.close, c.volume, c.timestamp AS candle_timestamp
        FROM indicators i
        LEFT JOIN candles c ON c.id = i.candle_id
        WHERE UPPER(i.symbol) = UPPER(?) AND i.interval = '15m'
        ORDER BY i.timestamp DESC
        LIMIT ?
        """,
        (symbol, limit),
    ).fetchall()


def _latest_candle(conn: sqlite3.Connection, symbol: str, interval: str = "15m") -> sqlite3.Row | None:
    if not _table_exists(conn, "candles"):
        return None
    return conn.execute(
        """
        SELECT *
        FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, interval),
    ).fetchone()


def _nearest_levels(conn: sqlite3.Connection, symbol: str, current_price: float) -> dict[str, Any]:
    try:
        from analysis.support_resistance import get_nearest_levels

        return get_nearest_levels(symbol, current_price, 1)
    except Exception:
        return {"support": [], "resistance": []}


def _patterns(symbol: str) -> list[dict[str, Any]]:
    try:
        from analysis.pattern_detector import run_all_patterns

        return run_all_patterns(symbol, interval="15m")
    except Exception:
        return []


def _news_features(conn: sqlite3.Connection, symbol: str) -> tuple[float, int, float]:
    today = _now_ist().date().isoformat()
    news_sentiment = 0.0
    has_announcement = 0
    announcement_impact = 0.0
    if _table_exists(conn, "news"):
        row = conn.execute(
            """
            SELECT AVG(sentiment_score) AS sentiment
            FROM news
            WHERE (UPPER(symbol) = UPPER(?) OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?)
              AND date(COALESCE(published_at, fetched_at)) = date(?)
            """,
            (symbol, f"%{symbol.upper()}%", today),
        ).fetchone()
        news_sentiment = _safe_float(row["sentiment"] if row else 0.0)
    if _table_exists(conn, "announcements"):
        row = conn.execute(
            """
            SELECT COUNT(*) AS count, MAX(COALESCE(signal_boost, impact_score, 0)) AS impact
            FROM announcements
            WHERE UPPER(symbol) = UPPER(?)
              AND date(COALESCE(published_at, fetched_at)) = date(?)
            """,
            (symbol, today),
        ).fetchone()
        has_announcement = 1 if row and int(row["count"] or 0) > 0 else 0
        announcement_impact = _safe_float(row["impact"] if row else 0.0)
    return news_sentiment, has_announcement, announcement_impact


def _nifty_momentum(conn: sqlite3.Connection) -> float:
    if not _table_exists(conn, "candles"):
        return 0.0
    for symbol in ("NIFTY", "NSEI", "^NSEI", "NIFTY50"):
        rows = conn.execute(
            """
            SELECT close
            FROM candles
            WHERE UPPER(symbol) = UPPER(?) AND interval = '15m'
            ORDER BY timestamp DESC
            LIMIT 4
            """,
            (symbol,),
        ).fetchall()
        if len(rows) >= 4:
            return _safe_pct(rows[0]["close"], rows[3]["close"])
    return 0.0


def _sector_momentum(conn: sqlite3.Connection, symbol: str) -> float:
    if not _table_exists(conn, "stock_universe") or not _table_exists(conn, "candles"):
        return 0.0
    sector_row = conn.execute(
        "SELECT sector FROM stock_universe WHERE UPPER(symbol) = UPPER(?) LIMIT 1",
        (symbol,),
    ).fetchone()
    if not sector_row or not sector_row["sector"]:
        return 0.0
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT c.symbol, c.close,
                   ROW_NUMBER() OVER (PARTITION BY c.symbol ORDER BY c.timestamp DESC) AS rn
            FROM candles c
            JOIN stock_universe s ON UPPER(s.symbol) = UPPER(c.symbol)
            WHERE s.sector = ? AND c.interval = '15m'
        ),
        previous AS (
            SELECT c.symbol, c.close,
                   ROW_NUMBER() OVER (PARTITION BY c.symbol ORDER BY c.timestamp DESC) AS rn
            FROM candles c
            JOIN stock_universe s ON UPPER(s.symbol) = UPPER(c.symbol)
            WHERE s.sector = ? AND c.interval = '15m'
        )
        SELECT AVG((l.close - p.close) / p.close * 100) AS momentum
        FROM latest l
        JOIN previous p ON p.symbol = l.symbol AND p.rn = 4
        WHERE l.rn = 1 AND p.close > 0
        """,
        (sector_row["sector"], sector_row["sector"]),
    ).fetchone()
    return _safe_float(rows["momentum"] if rows else 0.0)


def _timeframe_confluence_score(symbol: str, signal: dict[str, Any]) -> float:
    confluence = signal.get("timeframe_confluence")
    if isinstance(confluence, str):
        try:
            confluence = json.loads(confluence)
        except json.JSONDecodeError:
            confluence = None
    if isinstance(confluence, dict):
        score = confluence.get("confluence_score") or confluence.get("score")
        if score is not None:
            return _safe_float(score)
    try:
        from analysis.multi_timeframe import get_timeframe_confluence

        data = get_timeframe_confluence(symbol)
        raw = data.get("confluence_score", 0)
        return _safe_float(raw) * 25 if abs(_safe_float(raw)) <= 4 else _safe_float(raw)
    except Exception:
        return 0.0


def _account_context(conn: sqlite3.Connection) -> tuple[int, float, float, float]:
    current_open_trades = 0
    today_win_rate = 0.0
    recent_win_rate = 0.0
    daily_pnl_pct = 0.0
    today = _now_ist().date().isoformat()
    if _table_exists(conn, "paper_trades"):
        current_open_trades = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'"
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT outcome
            FROM paper_trades
            WHERE status = 'CLOSED' AND date(COALESCE(exit_time, updated_at)) = date(?)
            """,
            (today,),
        ).fetchall()
        if rows:
            today_win_rate = sum(row["outcome"] == "WIN" for row in rows) / len(rows) * 100
        recent = conn.execute(
            """
            SELECT outcome
            FROM paper_trades
            WHERE status = 'CLOSED'
            ORDER BY COALESCE(exit_time, updated_at, created_at) DESC
            LIMIT 10
            """
        ).fetchall()
        if recent:
            recent_win_rate = sum(row["outcome"] == "WIN" for row in recent) / len(recent) * 100
    if _table_exists(conn, "paper_account"):
        row = conn.execute("SELECT balance, daily_pnl FROM paper_account ORDER BY id LIMIT 1").fetchone()
        if row:
            balance = _safe_float(row["balance"])
            daily_pnl_pct = (_safe_float(row["daily_pnl"]) / balance * 100) if balance else 0.0
    return int(current_open_trades), today_win_rate, recent_win_rate, daily_pnl_pct


def _pattern_features(patterns: list[dict[str, Any]]) -> tuple[float, int, int]:
    if not patterns:
        return 0.0, 0, 0
    best = max(patterns, key=lambda item: _safe_float(item.get("strength")))
    direction = PATTERN_DIRECTION_ENCODING.get(str(best.get("direction", "NEUTRAL")).upper(), 0)
    return _safe_float(best.get("strength")), direction, 1


def _setup_features(setup_match: dict[str, Any]) -> tuple[float, float, int]:
    setup_info = setup_match.get("setup_info") if isinstance(setup_match.get("setup_info"), dict) else setup_match
    setup_type = str(setup_info.get("setup_type", "")).lower()
    return (
        _safe_float(setup_info.get("win_rate")),
        _safe_float(setup_match.get("match_quality") or setup_info.get("setup_match_quality")),
        SETUP_TYPE_ENCODING.get(setup_type, -1),
    )


def extract_features(symbol: str, signal: dict[str, Any], setup_match: dict[str, Any]) -> dict[str, float | int]:
    """Gather all ML features for one candidate trade."""
    normalized_symbol = symbol.upper().strip()
    now = _now_ist()
    with _connect() as conn:
        ensure_ml_schema(conn)
        indicators = _latest_indicator_rows(conn, normalized_symbol, 4)
        latest_indicator = indicators[0] if indicators else None
        candle = _latest_candle(conn, normalized_symbol)
        current_price = _safe_float(candle["close"] if candle else None)
        volume = _safe_float(candle["volume"] if candle else None)

        rsi = _safe_float(latest_indicator["rsi"] if latest_indicator else None)
        macd_hist = _safe_float(latest_indicator["macd_histogram"] if latest_indicator else None)
        ema20 = _safe_float(latest_indicator["ema_20"] if latest_indicator else None)
        ema50 = _safe_float(latest_indicator["ema_50"] if latest_indicator else None)
        vwap = _safe_float(latest_indicator["vwap"] if latest_indicator else None)
        volume_ratio = _safe_float(latest_indicator["volume_ratio"] if latest_indicator else None)
        rsi_3 = _safe_float(indicators[3]["rsi"] if len(indicators) >= 4 else rsi)
        previous_volume = _safe_float(indicators[1]["volume"] if len(indicators) >= 2 else volume)

        patterns = _patterns(normalized_symbol)
        best_pattern_strength, pattern_direction, chart_pattern_detected = _pattern_features(patterns)
        levels = _nearest_levels(conn, normalized_symbol, current_price)
        support = levels.get("support", [{}])[0] if levels.get("support") else {}
        resistance = levels.get("resistance", [{}])[0] if levels.get("resistance") else {}
        news_sentiment, has_announcement_today, announcement_impact = _news_features(conn, normalized_symbol)
        current_open_trades, today_win_rate, recent_win_rate, daily_pnl_pct = _account_context(conn)
        nifty_momentum = _nifty_momentum(conn)
        sector_momentum = _sector_momentum(conn, normalized_symbol)

    setup_win_rate, setup_match_quality, setup_type_encoded = _setup_features(setup_match or {})
    market_condition = str(signal.get("market_condition") or "SIDEWAYS").upper()
    current_time = datetime.combine(now.date(), now.time(), tzinfo=IST)
    market_open = datetime.combine(now.date(), MARKET_OPEN, tzinfo=IST)
    market_close = datetime.combine(now.date(), MARKET_CLOSE, tzinfo=IST)
    minutes_since_open = max(0, int((current_time - market_open).total_seconds() // 60))
    minutes_to_close = max(0, int((market_close - current_time).total_seconds() // 60))

    return {
        "rsi": rsi,
        "macd_hist": macd_hist,
        "ema20_vs_ema50_pct": _safe_pct(ema20, ema50),
        "price_vs_vwap_pct": _safe_pct(current_price, vwap),
        "volume_ratio": volume_ratio,
        "rsi_momentum_3bar": rsi - rsi_3,
        "volume_acceleration": _safe_pct(volume, previous_volume),
        "price_vs_ema20_pct": _safe_pct(current_price, ema20),
        "best_pattern_strength": best_pattern_strength,
        "pattern_direction": pattern_direction,
        "chart_pattern_detected": chart_pattern_detected,
        "distance_to_support_pct": _safe_float(support.get("distance_pct")),
        "distance_to_resistance_pct": _safe_float(resistance.get("distance_pct")),
        "at_support": 1 if _safe_float(support.get("distance_pct"), 999) <= 0.3 else 0,
        "at_resistance": 1 if _safe_float(resistance.get("distance_pct"), 999) <= 0.3 else 0,
        "support_strength": _safe_float(support.get("strength_score")),
        "resistance_strength": _safe_float(resistance.get("strength_score")),
        "setup_win_rate": setup_win_rate,
        "setup_match_quality": setup_match_quality,
        "setup_type_encoded": setup_type_encoded,
        "news_sentiment": news_sentiment,
        "has_announcement_today": has_announcement_today,
        "announcement_impact": announcement_impact,
        "hour": now.hour,
        "minute": now.minute,
        "day_of_week": now.weekday(),
        "minutes_since_open": int(minutes_since_open),
        "minutes_to_close": int(minutes_to_close),
        "market_condition_encoded": MARKET_CONDITION_ENCODING.get(market_condition, 0),
        "nifty_momentum": nifty_momentum,
        "sector_momentum": sector_momentum,
        "timeframe_confluence_score": _timeframe_confluence_score(normalized_symbol, signal),
        "current_open_trades": current_open_trades,
        "today_win_rate": today_win_rate,
        "recent_win_rate": recent_win_rate,
        "daily_pnl_pct": daily_pnl_pct,
    }


def store_trade_features(
    trade_id: int,
    symbol: str,
    features: dict[str, Any],
    win_probability: float | None = None,
    model_version: int | None = None,
) -> None:
    with _connect() as conn:
        ensure_ml_schema(conn)
        conn.execute(
            """
            INSERT INTO trade_features (
                trade_id, symbol, extracted_at, features_json, outcome,
                win_probability, model_version
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                trade_id,
                symbol.upper(),
                _now_ist().replace(microsecond=0).isoformat(),
                json.dumps(features, sort_keys=True),
                win_probability,
                model_version,
            ),
        )
        conn.commit()


def update_trade_feature_outcome(trade_id: int, outcome: str | int | None) -> None:
    if outcome is None:
        return
    if isinstance(outcome, str):
        outcome_value = 1 if outcome.upper() == "WIN" else 0
    else:
        outcome_value = 1 if int(outcome) else 0
    with _connect() as conn:
        ensure_ml_schema(conn)
        conn.execute(
            """
            UPDATE trade_features
            SET outcome = ?
            WHERE trade_id = ?
            """,
            (outcome_value, trade_id),
        )
        conn.commit()


def prepare_training_data() -> tuple[np.ndarray, np.ndarray]:
    """Return feature matrix X and outcome vector y from labeled trade features."""
    if not _load_ml_dependencies():
        raise RuntimeError(f"ML dependencies unavailable: {ML_DEPENDENCY_ERROR}")
    with _connect() as conn:
        ensure_ml_schema(conn)
        rows = conn.execute(
            """
            SELECT features_json, outcome
            FROM trade_features
            WHERE outcome IS NOT NULL
            ORDER BY id
            """
        ).fetchall()

    feature_rows = []
    labels = []
    for row in rows:
        try:
            payload = json.loads(row["features_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        feature_rows.append([_safe_float(payload.get(name), np.nan) for name in FEATURE_NAMES])
        labels.append(int(row["outcome"]))

    if not feature_rows:
        return np.empty((0, len(FEATURE_NAMES))), np.empty((0,), dtype=int)

    frame = pd.DataFrame(feature_rows, columns=FEATURE_NAMES)
    frame = frame.replace([np.inf, -np.inf], np.nan)
    imputer = SimpleImputer(strategy="median")
    X = imputer.fit_transform(frame)
    return X, np.array(labels, dtype=int)


def get_next_model_version() -> int:
    with _connect() as conn:
        ensure_ml_schema(conn)
        row = conn.execute("SELECT MAX(version) AS version FROM ml_models").fetchone()
    return int(row["version"] or 0) + 1


def save_model_metadata(
    version: int,
    cv_accuracy: float,
    model_file_path: str,
    top_features: list[tuple[str, float]],
    training_trades: int,
    test_accuracy: float | None = None,
    shadow_mode: int = 1,
    notes: str | None = None,
) -> None:
    with _connect() as conn:
        ensure_ml_schema(conn)
        conn.execute(
            """
            INSERT INTO ml_models (
                version, trained_at, training_trades, cv_accuracy, test_accuracy,
                feature_importance_json, model_file_path, is_active, shadow_mode, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                trained_at = excluded.trained_at,
                training_trades = excluded.training_trades,
                cv_accuracy = excluded.cv_accuracy,
                test_accuracy = excluded.test_accuracy,
                feature_importance_json = excluded.feature_importance_json,
                model_file_path = excluded.model_file_path,
                shadow_mode = excluded.shadow_mode,
                notes = excluded.notes
            """,
            (
                version,
                _now_ist().replace(microsecond=0).isoformat(),
                training_trades,
                cv_accuracy,
                test_accuracy,
                json.dumps(top_features),
                model_file_path,
                shadow_mode,
                notes,
            ),
        )
        conn.commit()


def train_model(X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Train and persist an XGBoost classifier."""
    if not _load_ml_dependencies():
        raise RuntimeError(f"ML dependencies unavailable: {ML_DEPENDENCY_ERROR}")
    if len(y) < 2 or len(set(y.tolist())) < 2:
        raise ValueError("Training requires at least two outcome classes")

    min_class_count = min(np.bincount(y))
    n_splits = min(5, int(min_class_count))
    if n_splits < 2:
        raise ValueError("Training requires at least two examples in each class")

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric="auc",
    )
    pipeline = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", model),
        ]
    )

    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    cv_scores = cross_val_score(pipeline, X, y, cv=cv, scoring="accuracy")

    test_accuracy = None
    if len(y) >= 20 and min_class_count >= 2:
        try:
            X_train, X_test, y_train, y_test = train_test_split(
                X,
                y,
                test_size=0.2,
                random_state=42,
                stratify=y,
            )
            pipeline.fit(X_train, y_train)
            test_accuracy = float(accuracy_score(y_test, pipeline.predict(X_test)))
        except ValueError:
            test_accuracy = None

    pipeline.fit(X, y)
    version = get_next_model_version()
    MODELS_DIR.mkdir(exist_ok=True)
    path = MODELS_DIR / f"trading_model_v{version}.pkl"
    joblib.dump({"pipeline": pipeline, "feature_names": FEATURE_NAMES}, path)

    xgb_model = pipeline.named_steps["model"]
    importance = dict(zip(FEATURE_NAMES, xgb_model.feature_importances_))
    top_features = [
        (name, float(value))
        for name, value in sorted(importance.items(), key=lambda item: item[1], reverse=True)[:10]
    ]
    cv_accuracy = float(cv_scores.mean())
    save_model_metadata(
        version,
        cv_accuracy,
        str(path),
        top_features,
        training_trades=len(y),
        test_accuracy=test_accuracy,
        shadow_mode=1,
        notes="Shadow mode pending activation review",
    )
    return {"version": version, "cv_accuracy": cv_accuracy, "test_accuracy": test_accuracy, "top_features": top_features}


def _active_or_shadow_model_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM ml_models
        WHERE is_active = 1 OR shadow_mode = 1
        ORDER BY is_active DESC, version DESC
        LIMIT 1
        """
    ).fetchone()


def _feature_vector(features: dict[str, Any]) -> np.ndarray:
    return np.array([[_safe_float(features.get(name), 0.0) for name in FEATURE_NAMES]], dtype=float)


def _rule_based_score(features: dict[str, Any]) -> float:
    pieces = [
        _safe_float(features.get("setup_match_quality")) / 100,
        _safe_float(features.get("setup_win_rate")) / 100,
        min(1.0, max(0.0, _safe_float(features.get("timeframe_confluence_score")) / 100)),
        1.0 if features.get("at_support") else 0.0,
        max(0.0, min(1.0, (_safe_float(features.get("news_sentiment")) + 1) / 2)),
    ]
    return float(sum(pieces) / len(pieces))


def _confidence(probability: float) -> str:
    if probability >= 0.68:
        return "HIGH"
    if probability >= 0.55:
        return "MEDIUM"
    return "LOW"


def _top_factors(row: sqlite3.Row | None, features: dict[str, Any], limit: int = 3) -> list[dict[str, float | str]]:
    if row is None or not row["feature_importance_json"]:
        ranked = sorted(
            ((name, abs(_safe_float(features.get(name)))) for name in FEATURE_NAMES),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]
        return [{"feature": name, "value": _safe_float(features.get(name))} for name, _ in ranked]
    try:
        importance = json.loads(row["feature_importance_json"])
    except json.JSONDecodeError:
        importance = []
    return [
        {"feature": name, "importance": _safe_float(score), "value": _safe_float(features.get(name))}
        for name, score in importance[:limit]
    ]


def predict(symbol: str, features: dict[str, Any], log_prediction: bool = True) -> dict[str, Any]:
    """Predict win probability with active/shadow model or fallback rules."""
    normalized_symbol = symbol.upper().strip()
    with _connect() as conn:
        ensure_ml_schema(conn)
        row = _active_or_shadow_model_row(conn)
        if row is None:
            rule_score = _rule_based_score(features)
            return {
                "win_probability": rule_score,
                "should_trade": rule_score > TRADE_THRESHOLD,
                "confidence": _confidence(rule_score),
                "top_factors": _top_factors(None, features),
                "model_version": None,
                "shadow_mode": False,
                "fallback": True,
                "fallback_used": True,
                "rule_based_score": rule_score,
                "fallback_reason": "no active or shadow model",
            }

        if not _load_ml_dependencies():
            rule_score = _rule_based_score(features)
            return {
                "win_probability": rule_score,
                "should_trade": rule_score > TRADE_THRESHOLD,
                "confidence": _confidence(rule_score),
                "top_factors": _top_factors(None, features),
                "model_version": int(row["version"]),
                "shadow_mode": bool(row["shadow_mode"]),
                "fallback": True,
                "fallback_used": True,
                "rule_based_score": rule_score,
                "fallback_reason": f"ML dependencies unavailable: {ML_DEPENDENCY_ERROR}",
            }

        artifact = joblib.load(row["model_file_path"])
        pipeline = artifact["pipeline"] if isinstance(artifact, dict) else artifact
        win_probability = float(pipeline.predict_proba(_feature_vector(features))[0][1])
        if bool(row["shadow_mode"]) and log_prediction:
            conn.execute(
                """
                INSERT INTO trade_features (
                    trade_id, symbol, extracted_at, features_json, outcome,
                    win_probability, model_version
                ) VALUES (NULL, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    normalized_symbol,
                    _now_ist().replace(microsecond=0).isoformat(),
                    json.dumps(features, sort_keys=True),
                    win_probability,
                    row["version"],
                ),
            )
            conn.commit()

    shadow_mode = bool(row["shadow_mode"])
    return {
        "win_probability": win_probability,
        "should_trade": bool(win_probability > TRADE_THRESHOLD),
        "confidence": _confidence(win_probability),
        "top_factors": _top_factors(row, features),
        "model_version": int(row["version"]),
        "shadow_mode": shadow_mode,
        "fallback": False,
        "fallback_used": False,
    }


def activate_model_if_ready() -> dict[str, Any]:
    """Activate latest shadow model if it beats rule-based decisions."""
    with _connect() as conn:
        ensure_ml_schema(conn)
        model_row = conn.execute(
            """
            SELECT *
            FROM ml_models
            WHERE shadow_mode = 1
            ORDER BY version DESC
            LIMIT 1
            """
        ).fetchone()
        if not model_row:
            return {"activated": False, "reason": "no shadow model"}
        if _safe_float(model_row["cv_accuracy"]) <= 0.58:
            return {"activated": False, "reason": "cv accuracy below activation threshold"}

        rows = conn.execute(
            """
            SELECT features_json, outcome, win_probability
            FROM trade_features
            WHERE outcome IS NOT NULL
              AND model_version = ?
              AND win_probability IS NOT NULL
            """,
            (model_row["version"],),
        ).fetchall()
        if len(rows) < 10:
            return {"activated": False, "reason": "not enough shadow predictions"}

        model_correct = 0
        rule_correct = 0
        for row in rows:
            outcome = int(row["outcome"])
            model_prediction = 1 if _safe_float(row["win_probability"]) > TRADE_THRESHOLD else 0
            try:
                features = json.loads(row["features_json"] or "{}")
            except json.JSONDecodeError:
                features = {}
            rule_prediction = 1 if _rule_based_score(features) > TRADE_THRESHOLD else 0
            model_correct += int(model_prediction == outcome)
            rule_correct += int(rule_prediction == outcome)

        shadow_accuracy = model_correct / len(rows)
        rule_accuracy = rule_correct / len(rows)
        if shadow_accuracy <= rule_accuracy:
            return {
                "activated": False,
                "reason": "shadow model did not beat rule baseline",
                "shadow_accuracy": shadow_accuracy,
                "rule_accuracy": rule_accuracy,
            }

        conn.execute("UPDATE ml_models SET is_active = 0")
        conn.execute(
            """
            UPDATE ml_models
            SET is_active = 1,
                shadow_mode = 0,
                notes = ?
            WHERE id = ?
            """,
            (
                f"ML model v{model_row['version']} activated - {shadow_accuracy:.2%} accuracy vs {rule_accuracy:.2%} rules",
                model_row["id"],
            ),
        )
        conn.commit()

    message = (
        f"ML model v{model_row['version']} activated - "
        f"{shadow_accuracy:.2%} accuracy vs {rule_accuracy:.2%} rules"
    )
    log.info(message)
    return {
        "activated": True,
        "version": int(model_row["version"]),
        "shadow_accuracy": shadow_accuracy,
        "rule_accuracy": rule_accuracy,
        "message": message,
    }


def run_training_pipeline() -> dict[str, Any]:
    """Prepare data, train a model, and leave it in shadow mode if viable."""
    if not _load_ml_dependencies():
        return {
            "trained": False,
            "reason": f"ML dependencies unavailable: {ML_DEPENDENCY_ERROR}",
            "training_trades": 0,
        }
    X, y = prepare_training_data()
    if len(y) < MIN_TRAINING_TRADES:
        return {
            "trained": False,
            "reason": f"need {MIN_TRAINING_TRADES} labeled trades, have {len(y)}",
            "training_trades": int(len(y)),
        }
    result = train_model(X, y)
    if result["cv_accuracy"] <= 0.55:
        with _connect() as conn:
            conn.execute(
                "UPDATE ml_models SET shadow_mode = 0, is_active = 0, notes = ? WHERE version = ?",
                ("Rejected: CV accuracy did not clear 0.55", result["version"]),
            )
            conn.commit()
        return {"trained": True, "shadow_mode": False, "accepted": False, **result}
    activation = activate_model_if_ready()
    return {"trained": True, "shadow_mode": True, "accepted": True, "activation": activation, **result}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    print(run_training_pipeline())
