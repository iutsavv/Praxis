"""
init_db.py — Database initialiser for the AI Paper Trading Agent.

Creates a SQLite database (trading.db) inside the database/ folder with all tables
and seeds it with an initial watchlist of 15 liquid NSE symbols plus a default
strategy-rules row and a default paper-account row when needed.

Run directly:
    python database/init_db.py
"""

import sqlite3
import os
import json
import sys
from datetime import datetime


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "trading.db")


# ---------------------------------------------------------------------------
# Table definitions
# ---------------------------------------------------------------------------

EXPECTED_SCHEMAS: dict[str, list[dict[str, object]]] = {
    "paper_account": [
        {"name": "id", "type": "INTEGER", "notnull": 0, "default": None, "pk": 1},
        {"name": "balance", "type": "REAL", "notnull": 0, "default": "100000", "pk": 0},
        {"name": "initial_balance", "type": "REAL", "notnull": 0, "default": "100000", "pk": 0},
        {"name": "total_pnl", "type": "REAL", "notnull": 0, "default": "0", "pk": 0},
        {"name": "daily_pnl", "type": "REAL", "notnull": 0, "default": "0", "pk": 0},
        {"name": "total_trades", "type": "INTEGER", "notnull": 0, "default": "0", "pk": 0},
        {"name": "winning_trades", "type": "INTEGER", "notnull": 0, "default": "0", "pk": 0},
        {"name": "losing_trades", "type": "INTEGER", "notnull": 0, "default": "0", "pk": 0},
        {"name": "win_rate", "type": "REAL", "notnull": 0, "default": "0", "pk": 0},
        {"name": "max_drawdown", "type": "REAL", "notnull": 0, "default": "0", "pk": 0},
        {"name": "peak_balance", "type": "REAL", "notnull": 0, "default": "100000", "pk": 0},
        {"name": "open_positions_count", "type": "INTEGER", "notnull": 0, "default": "0", "pk": 0},
        {"name": "last_updated", "type": "TEXT", "notnull": 0, "default": "datetime('now')", "pk": 0},
    ],
    "trade_logs": [
        {"name": "id", "type": "INTEGER", "notnull": 0, "default": None, "pk": 1},
        {"name": "timestamp", "type": "TEXT", "notnull": 0, "default": "datetime('now')", "pk": 0},
        {"name": "symbol", "type": "TEXT", "notnull": 1, "default": None, "pk": 0},
        {"name": "direction", "type": "TEXT", "notnull": 0, "default": None, "pk": 0},
        {"name": "score", "type": "REAL", "notnull": 0, "default": None, "pk": 0},
        {"name": "approved", "type": "INTEGER", "notnull": 0, "default": None, "pk": 0},
        {"name": "reason", "type": "TEXT", "notnull": 0, "default": None, "pk": 0},
    ],
}

TABLES: dict[str, str] = {
    "stocks": """
        CREATE TABLE IF NOT EXISTS stocks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT    NOT NULL UNIQUE,
            name            TEXT    NOT NULL,
            sector          TEXT    NOT NULL,
            is_active       INTEGER NOT NULL DEFAULT 1,   -- 1 = active, 0 = inactive
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """,

    "candles": """
        CREATE TABLE IF NOT EXISTS candles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT    NOT NULL,
            interval        TEXT    NOT NULL,              -- '15m' or '1h'
            open            REAL    NOT NULL,
            high            REAL    NOT NULL,
            low             REAL    NOT NULL,
            close           REAL    NOT NULL,
            volume          INTEGER NOT NULL,
            timestamp       TEXT    NOT NULL,              -- ISO-8601 string
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(symbol, interval, timestamp)
        );
    """,

    "price_levels": """
        CREATE TABLE IF NOT EXISTS price_levels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            level_price     REAL NOT NULL,
            level_type      TEXT NOT NULL,
            touches         INTEGER DEFAULT 0,
            strength_score  REAL DEFAULT 0,
            avg_volume_at_touch REAL,
            first_touch     TEXT,
            last_touch      TEXT,
            broken_at       TEXT,
            is_active       INTEGER DEFAULT 1,
            last_updated    TEXT,
            UNIQUE(symbol, timeframe, level_price, level_type)
        );
    """,

    "patterns": """
        CREATE TABLE IF NOT EXISTS patterns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            interval        TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            pattern_name    TEXT NOT NULL,
            pattern_type    TEXT NOT NULL,
            direction       TEXT NOT NULL,
            strength        REAL DEFAULT 50,
            volume_confirmed INTEGER DEFAULT 0,
            candles_used    INTEGER DEFAULT 1,
            detected_at     TEXT DEFAULT (datetime('now')),
            UNIQUE(symbol, interval, timestamp, pattern_name)
        );
    """,

    "indicators": """
        CREATE TABLE IF NOT EXISTS indicators (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            candle_id       INTEGER NOT NULL,
            symbol          TEXT    NOT NULL,
            interval        TEXT    NOT NULL,
            rsi             REAL,
            macd            REAL,
            macd_signal     REAL,
            macd_histogram  REAL,
            ema_20          REAL,
            ema_50          REAL,
            vwap            REAL,
            volume_ratio    REAL,                         -- today vol / 10-day avg vol
            timestamp       TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (candle_id) REFERENCES candles(id)
        );
    """,

    "signal_scores": """
        CREATE TABLE IF NOT EXISTS signal_scores (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT    NOT NULL,
            scan_cycle          TEXT    NOT NULL,          -- ISO-8601 of the scan run
            confidence_score    REAL    NOT NULL CHECK(confidence_score BETWEEN 0 AND 100),
            direction           TEXT    NOT NULL CHECK(direction IN ('BUY', 'SELL', 'HOLD')),
            market_condition    TEXT    NOT NULL CHECK(market_condition IN ('TRENDING', 'SIDEWAYS', 'VOLATILE')),
            contrib_rsi         REAL,
            contrib_macd        REAL,
            contrib_volume      REAL,
            contrib_vwap        REAL,
            explanation         TEXT,
            notes               TEXT,
            timeframe_confluence TEXT,
            announcement_boost  REAL DEFAULT 0,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """,

    "paper_trades": """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT    NOT NULL,
            direction           TEXT    NOT NULL CHECK(direction IN ('BUY', 'SELL')),
            quantity            INTEGER NOT NULL DEFAULT 1,

            -- Entry details
            entry_price         REAL    NOT NULL,
            entry_time          TEXT    NOT NULL,
            entry_reason        TEXT,
            entry_rsi           REAL,
            entry_macd          REAL,
            entry_macd_signal   REAL,
            entry_macd_histogram REAL,
            entry_ema_20        REAL,
            entry_ema_50        REAL,
            entry_vwap          REAL,
            entry_volume_ratio  REAL,
            rsi_score           REAL,
            macd_score          REAL,
            volume_score        REAL,
            vwap_score          REAL,

            -- Risk management
            target_price        REAL,
            stop_loss_price     REAL,

            -- Exit details
            exit_price          REAL,
            exit_time           TEXT,
            exit_reason         TEXT CHECK(exit_reason IN ('TARGET', 'STOPLOSS', 'EOD') OR exit_reason IS NULL),

            -- Outcome
            pnl                 REAL,
            outcome             TEXT CHECK(outcome IN ('WIN', 'LOSS', 'BREAKEVEN') OR outcome IS NULL),
            status              TEXT    NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED')),

            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """,

    "strategy_rules": """
        CREATE TABLE IF NOT EXISTS strategy_rules (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            version             INTEGER NOT NULL UNIQUE,
            is_active           INTEGER NOT NULL DEFAULT 1,

            -- Signal weights (must sum to 1.0)
            weight_rsi          REAL    NOT NULL DEFAULT 0.25,
            weight_macd         REAL    NOT NULL DEFAULT 0.25,
            weight_volume       REAL    NOT NULL DEFAULT 0.25,
            weight_vwap         REAL    NOT NULL DEFAULT 0.25,

            -- Trade parameters
            min_score_to_trade  REAL    NOT NULL DEFAULT 60,
            max_open_trades     INTEGER NOT NULL DEFAULT 3,
            risk_per_trade_pct  REAL    NOT NULL DEFAULT 1.0,

            -- Best entry window (e.g. "09:30-10:30")
            best_entry_window   TEXT,
            trade_in_sideways   INTEGER NOT NULL DEFAULT 1,
            notes               TEXT,

            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """,

    "learning_insights": """
        CREATE TABLE IF NOT EXISTS learning_insights (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start              TEXT    NOT NULL,      -- Monday ISO date
            week_end                TEXT    NOT NULL,      -- Friday ISO date
            total_trades            INTEGER,
            win_rate                REAL,                  -- 0.0 – 1.0
            best_time_window        TEXT,
            best_market_condition   TEXT,
            worst_signal            TEXT,
            findings_json           TEXT,                  -- full analysis as JSON
            summary                 TEXT,                  -- human-readable summary
            trades_analyzed         INTEGER,
            strategy_version        INTEGER,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """,

    "paper_account": """
        CREATE TABLE IF NOT EXISTS paper_account (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            balance              REAL DEFAULT 100000,
            initial_balance      REAL DEFAULT 100000,
            total_pnl            REAL DEFAULT 0,
            daily_pnl            REAL DEFAULT 0,
            total_trades         INTEGER DEFAULT 0,
            winning_trades       INTEGER DEFAULT 0,
            losing_trades        INTEGER DEFAULT 0,
            win_rate             REAL DEFAULT 0,
            max_drawdown         REAL DEFAULT 0,
            peak_balance         REAL DEFAULT 100000,
            open_positions_count INTEGER DEFAULT 0,
            last_updated         TEXT DEFAULT (datetime('now'))
        );
    """,

    "trade_logs": """
        CREATE TABLE IF NOT EXISTS trade_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT DEFAULT (datetime('now')),
            symbol      TEXT NOT NULL,
            direction   TEXT,
            score       REAL,
            approved    INTEGER,      -- 0 or 1
            reason      TEXT
        );
    """,

    "stock_universe": """
        CREATE TABLE IF NOT EXISTS stock_universe (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT UNIQUE NOT NULL,
            name                TEXT,
            sector              TEXT,
            industry            TEXT,
            series              TEXT,
            market_cap          REAL,
            avg_daily_volume    REAL,
            is_fo_stock         INTEGER DEFAULT 0,
            is_large_cap        INTEGER DEFAULT 0,
            is_mid_cap          INTEGER DEFAULT 0,
            is_small_cap        INTEGER DEFAULT 0,
            is_active           INTEGER DEFAULT 1,
            in_stage2_scan      INTEGER DEFAULT 0,
            listing_date        TEXT,
            last_updated        TEXT,
            added_at            TEXT DEFAULT (datetime('now'))
        );
    """,

    "scan_results": """
        CREATE TABLE IF NOT EXISTS scan_results (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT NOT NULL,
            scan_time           TEXT NOT NULL,
            stage               INTEGER NOT NULL,
            stage1_flagged      INTEGER DEFAULT 0,
            stage1_price_change REAL,
            stage1_volume_ratio REAL,
            stage1_reason       TEXT,
            stage2_score        REAL,
            stage2_direction    TEXT,
            stage2_pattern      TEXT,
            stage2_setup        TEXT,
            final_selected      INTEGER DEFAULT 0,
            UNIQUE(symbol, scan_time)
        );
    """,

    "announcements": """
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
        );
    """,

    "news": """
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
            UNIQUE(url)
        );
    """,

    "api_usage": """
        CREATE TABLE IF NOT EXISTS api_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            api_name    TEXT,
            call_date   TEXT,
            call_count  INTEGER DEFAULT 0,
            UNIQUE(api_name, call_date)
        );
    """,

    "setups": """
        CREATE TABLE IF NOT EXISTS setups (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_code              TEXT UNIQUE NOT NULL,
            setup_name              TEXT NOT NULL,
            setup_type              TEXT NOT NULL,
            direction_bias          TEXT NOT NULL,
            conditions_json         TEXT NOT NULL,
            entry_method            TEXT NOT NULL,
            target_method           TEXT NOT NULL,
            stoploss_method         TEXT NOT NULL,
            best_market_condition   TEXT,
            avoid_market_condition  TEXT,
            best_time_start         TEXT,
            best_time_end           TEXT,
            min_timeframe_confluence INTEGER DEFAULT 2,
            total_trades            INTEGER DEFAULT 0,
            win_rate                REAL DEFAULT 0,
            avg_win_pct             REAL DEFAULT 0,
            avg_loss_pct            REAL DEFAULT 0,
            profit_factor           REAL DEFAULT 0,
            best_stock              TEXT,
            is_active               INTEGER DEFAULT 1,
            created_at              TEXT DEFAULT (datetime('now')),
            last_updated            TEXT
        );
    """,

    "trade_features": """
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
        );
    """,

    "ml_models": """
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
        );
    """,

    "kronos_forecasts": """
        CREATE TABLE IF NOT EXISTS kronos_forecasts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT NOT NULL,
            scan_time           TEXT,
            interval            TEXT NOT NULL,
            lookback            INTEGER NOT NULL,
            pred_len            INTEGER NOT NULL,
            model_name          TEXT NOT NULL,
            last_close          REAL,
            predicted_close     REAL,
            expected_return_pct REAL,
            forecast_direction  TEXT,
            score_adjustment    REAL DEFAULT 0,
            confirms_signal     INTEGER DEFAULT 0,
            message             TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """,

    "agent_memory": """
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
        );
    """,

    "agent_debates": """
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
        );
    """,

    "trade_reviews": """
        CREATE TABLE IF NOT EXISTS trade_reviews (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id            INTEGER NOT NULL UNIQUE,
            symbol              TEXT NOT NULL,
            outcome             TEXT,
            pnl                 REAL,
            pnl_pct             REAL,
            review_json         TEXT,
            created_at          TEXT NOT NULL
        );
    """,

    "fundamental_documents": """
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
        );
    """,

    "fundamental_facts": """
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
        );
    """,

    "fundamental_agent_reports": """
        CREATE TABLE IF NOT EXISTS fundamental_agent_reports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT NOT NULL,
            recommendation  TEXT NOT NULL,
            confidence      REAL DEFAULT 0,
            report_json     TEXT,
            created_at      TEXT NOT NULL
        );
    """,

    "sec_company_map": """
        CREATE TABLE IF NOT EXISTS sec_company_map (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL UNIQUE,
            cik             TEXT NOT NULL,
            title           TEXT,
            last_updated    TEXT NOT NULL
        );
    """,
}


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

WATCHLIST = [
    ("RELIANCE",   "Reliance Industries Ltd",          "Oil & Gas / Conglomerate"),
    ("ICICIBANK",  "ICICI Bank Ltd",                    "Banking"),
    ("HDFCBANK",   "HDFC Bank Ltd",                     "Banking"),
    ("INFY",       "Infosys Ltd",                       "Information Technology"),
    ("TCS",        "Tata Consultancy Services Ltd",     "Information Technology"),
    ("AXISBANK",   "Axis Bank Ltd",                     "Banking"),
    ("SBIN",       "State Bank of India",               "Banking"),
    ("BAJFINANCE", "Bajaj Finance Ltd",                 "Financial Services"),
    ("TMCV",       "Tata Motors Ltd (Commercial Vehicles)", "Automobile"),
    ("MARUTI",     "Maruti Suzuki India Ltd",           "Automobile"),
    ("WIPRO",      "Wipro Ltd",                         "Information Technology"),
    ("HINDUNILVR", "Hindustan Unilever Ltd",            "FMCG"),
    ("SUNPHARMA",  "Sun Pharmaceutical Industries Ltd", "Pharmaceuticals"),
    ("INDIGO",     "InterGlobe Aviation Ltd",           "Aviation"),
    ("ADANIPORTS", "Adani Ports and SEZ Ltd",           "Infrastructure"),
]


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    )
    return cursor.fetchone() is not None


def _normalise_default(default_value: object) -> object:
    if default_value is None:
        return None

    value = str(default_value).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return value.strip("'\"").lower()


def _normalise_type(type_value: object) -> str:
    return " ".join(str(type_value or "").upper().split())


def _get_table_schema(cursor: sqlite3.Cursor, table_name: str) -> list[dict[str, object]]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [
        {
            "name": row[1],
            "type": _normalise_type(row[2]),
            "notnull": int(row[3]),
            "default": _normalise_default(row[4]),
            "pk": int(row[5]),
        }
        for row in cursor.fetchall()
    ]


def _schema_mismatches(
    actual_schema: list[dict[str, object]],
    expected_schema: list[dict[str, object]],
) -> list[str]:
    mismatches: list[str] = []
    actual_by_name = {str(column["name"]): column for column in actual_schema}
    expected_by_name = {str(column["name"]): column for column in expected_schema}

    for expected_index, expected_column in enumerate(expected_schema):
        column_name = str(expected_column["name"])
        actual_column = actual_by_name.get(column_name)

        if actual_column is None:
            mismatches.append(f"missing column '{column_name}'")
            continue

        actual_index = actual_schema.index(actual_column)
        if actual_index != expected_index:
            mismatches.append(
                f"column '{column_name}' order differs: expected position "
                f"{expected_index}, found {actual_index}"
            )

        comparisons = {
            "type": (_normalise_type(expected_column["type"]), actual_column["type"]),
            "notnull": (int(expected_column["notnull"]), actual_column["notnull"]),
            "default": (
                _normalise_default(expected_column["default"]),
                actual_column["default"],
            ),
            "pk": (int(expected_column["pk"]), actual_column["pk"]),
        }
        for field_name, (expected_value, actual_value) in comparisons.items():
            if expected_value != actual_value:
                mismatches.append(
                    f"column '{column_name}' {field_name} differs: "
                    f"expected {expected_value!r}, found {actual_value!r}"
                )

    extra_columns = sorted(set(actual_by_name) - set(expected_by_name))
    for column_name in extra_columns:
        mismatches.append(f"extra column '{column_name}'")

    return mismatches


def _create_checked_table(cursor: sqlite3.Cursor, table_name: str) -> None:
    existed_before = _table_exists(cursor, table_name)

    if existed_before:
        mismatches = _schema_mismatches(
            _get_table_schema(cursor, table_name),
            EXPECTED_SCHEMAS[table_name],
        )
        if mismatches:
            print(f"\n⚠️ {table_name} schema mismatch:")
            for mismatch in mismatches:
                print(f"   - {mismatch}")
            print(f"⚠️ {table_name} exists but schema differs — see mismatch above")
        else:
            print(f"✅ {table_name} already exists and matches schema")

    cursor.execute(TABLES[table_name])

    if not existed_before:
        print(f"✅ {table_name} created fresh")


def init_db() -> None:
    """Create the trading.db database and all tables."""

    # Ensure the database directory exists
    os.makedirs(DB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Enable WAL mode for better concurrent-read performance
    cursor.execute("PRAGMA journal_mode=WAL;")

    for table_name in EXPECTED_SCHEMAS:
        _create_checked_table(cursor, table_name)

    for table_name, ddl in TABLES.items():
        if table_name in EXPECTED_SCHEMAS:
            continue

        cursor.execute(ddl)
        print(f"  [OK] Table '{table_name}' created successfully.")

    conn.commit()
    conn.close()

    print(f"\n  [OK] Database initialised at: {DB_PATH}")
    print(f"       Official schema tables: {len(TABLES)}\n")


def seed_watchlist() -> None:
    """Seed the stocks table with the 15 liquid NSE symbols and
    insert a default row into strategy_rules (version 1)."""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ── Seed stocks ──────────────────────────────────────────────
    inserted = 0
    skipped = 0
    for symbol, name, sector in WATCHLIST:
        try:
            cursor.execute(
                """
                INSERT INTO stocks (symbol, name, sector, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (symbol, name, sector),
            )
            inserted += 1
            print(f"  [OK] Seeded stock: {symbol:<12} - {name}")
        except sqlite3.IntegrityError:
            skipped += 1
            print(f"  [SKIP] Skipped (already exists): {symbol}")

    print(f"\n  [INFO] Stocks seeded: {inserted} inserted, {skipped} skipped.\n")

    # ── Seed default strategy rules ──────────────────────────────
    try:
        cursor.execute(
            """
            INSERT INTO strategy_rules (
                version, is_active,
                weight_rsi, weight_macd, weight_volume, weight_vwap,
                min_score_to_trade, max_open_trades, risk_per_trade_pct,
                best_entry_window
            ) VALUES (
                1, 1,
                0.25, 0.25, 0.25, 0.25,
                60, 3, 1.0,
                '09:30-10:30'
            )
            """,
        )
        print("  [OK] Default strategy rules (v1) inserted.")
    except sqlite3.IntegrityError:
        print("  [SKIP] Strategy rules v1 already exists - skipped.")

    conn.commit()
    conn.close()

    print("\n  [OK] Seeding complete.\n")


def seed_paper_account() -> None:
    """Create one default paper-account row only when the table is empty."""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM paper_account")
    account_count = cursor.fetchone()[0]

    if account_count == 0:
        cursor.execute(
            """
            INSERT INTO paper_account (
                balance,
                initial_balance,
                total_pnl,
                daily_pnl,
                total_trades,
                winning_trades,
                losing_trades,
                win_rate,
                max_drawdown,
                peak_balance,
                open_positions_count
            ) VALUES (
                100000,
                100000,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                100000,
                0
            )
            """,
        )
        print(
            "  [OK] Seeded paper_account with default balance ₹1,00,000. "
            "NOTE: audit found the live value is ₹10,00,000, so decide "
            "whether that 10x mismatch is intentional."
        )
    else:
        print(
            f"  [SKIP] paper_account already has {account_count} row(s) - "
            "no duplicate inserted."
        )

    conn.commit()
    conn.close()


def seed_setups() -> None:
    """Seed the mathematically defined setup library."""
    try:
        from brain.setup_library import seed_initial_setups

        result = seed_initial_setups()
        print(
            "  [OK] Setups seeded: "
            f"{result['inserted']} inserted, {result['skipped']} existing."
        )
    except Exception as exc:
        print(f"  [WARN] Could not seed setup library: {exc}")


def print_table_summary() -> None:
    """Print row counts for every official schema table."""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("=" * 60)
    print("  Database table summary")
    print("=" * 60)
    for table_name in TABLES:
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cursor.fetchone()[0]
        print(f"  {table_name:<22} {row_count:>8} row(s)")

    conn.close()
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  AI Paper Trading Agent — Database Initialisation")
    print("=" * 60 + "\n")

    init_db()
    seed_watchlist()
    seed_paper_account()
    seed_setups()
    print_table_summary()

    print("=" * 60)
    print("  All done!  Database is ready.")
    print("=" * 60 + "\n")
