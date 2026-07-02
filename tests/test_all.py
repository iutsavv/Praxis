"""
test_all.py — End-to-end test suite for the AI Paper Trading Agent.

Uses only Python's built-in `unittest` module.  Tests are ordered across
blocks so that later blocks can rely on state created by earlier ones
(e.g. the fetcher tests assume the DB already exists).

Run:
    python -m unittest tests.test_all -v
    # or simply:
    python tests/test_all.py
"""

import os
import sys
import sqlite3
import unittest

# ---------------------------------------------------------------------------
# Make sure the project root is on sys.path so we can import our modules
# regardless of where the test runner is invoked from.
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "database", "trading.db")

# Expected tables (all 7)
EXPECTED_TABLES = [
    "stocks",
    "candles",
    "indicators",
    "signal_scores",
    "paper_trades",
    "strategy_rules",
    "learning_insights",
]


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _connect() -> sqlite3.Connection:
    """Return a read-only connection to trading.db."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Block 1 — Database tests (init_db.py)
# ═══════════════════════════════════════════════════════════════════════════

class Test_01_Database(unittest.TestCase):
    """Verify that init_db.py created the database, tables, and seed data."""

    # ------------------------------------------------------------------
    # 1.1  trading.db file must exist
    # ------------------------------------------------------------------
    def test_01_db_file_exists(self):
        """trading.db should exist after init_db has been run."""
        self.assertTrue(
            os.path.isfile(DB_PATH),
            f"Database file not found at {DB_PATH}. "
            "Run  python database/init_db.py  first.",
        )

    # ------------------------------------------------------------------
    # 1.2  All 7 tables must be present
    # ------------------------------------------------------------------
    def test_02_all_tables_exist(self):
        """All 7 expected tables should be present in the database."""
        conn = _connect()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        actual_tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        for table in EXPECTED_TABLES:
            with self.subTest(table=table):
                self.assertIn(
                    table,
                    actual_tables,
                    f"Table '{table}' is missing from the database.",
                )

    # ------------------------------------------------------------------
    # 1.3  stocks table has exactly 15 seeded rows
    # ------------------------------------------------------------------
    def test_03_stocks_count(self):
        """The stocks table should contain exactly 15 seeded rows."""
        conn = _connect()
        cursor = conn.execute("SELECT COUNT(*) FROM stocks")
        count = cursor.fetchone()[0]
        conn.close()

        self.assertEqual(
            count, 15,
            f"Expected 15 stocks, found {count}.",
        )

    # ------------------------------------------------------------------
    # 1.4  strategy_rules has one active row
    # ------------------------------------------------------------------
    def test_04_strategy_rules_active(self):
        """strategy_rules should have exactly one row with is_active = 1."""
        conn = _connect()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM strategy_rules WHERE is_active = 1"
        )
        count = cursor.fetchone()[0]
        conn.close()

        self.assertEqual(
            count, 1,
            f"Expected 1 active strategy rule, found {count}.",
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Block 2 — Fetcher tests (data/fetcher.py)
# ═══════════════════════════════════════════════════════════════════════════

class Test_02_Fetcher(unittest.TestCase):
    """Verify that fetcher.py correctly populated the candles table."""

    # ------------------------------------------------------------------
    # 2.1  candles table is not empty
    # ------------------------------------------------------------------
    def test_01_candles_not_empty(self):
        """The candles table should contain at least one row after the
        fetcher has been run."""
        conn = _connect()
        cursor = conn.execute("SELECT COUNT(*) FROM candles")
        count = cursor.fetchone()[0]
        conn.close()

        self.assertGreater(
            count, 0,
            "candles table is empty.  Run  python data/fetcher.py  first.",
        )

    # ------------------------------------------------------------------
    # 2.2  Every active symbol has at least one candle
    # ------------------------------------------------------------------
    def test_02_every_symbol_has_candles(self):
        """Each active symbol in stocks should have >= 1 row in candles."""
        conn = _connect()

        # Get active symbols
        cursor = conn.execute(
            "SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol"
        )
        active_symbols = [row[0] for row in cursor.fetchall()]

        # Get symbols that actually have candle data
        cursor = conn.execute(
            "SELECT DISTINCT symbol FROM candles"
        )
        candle_symbols = {row[0] for row in cursor.fetchall()}
        conn.close()

        for symbol in active_symbols:
            with self.subTest(symbol=symbol):
                self.assertIn(
                    symbol,
                    candle_symbols,
                    f"No candle data found for active symbol '{symbol}'.",
                )

    # ------------------------------------------------------------------
    # 2.3  No NULL values in critical OHLCV columns
    # ------------------------------------------------------------------
    def test_03_no_null_ohlcv(self):
        """No candle row should have NULL in open, high, low, close,
        volume, or timestamp."""
        conn = _connect()

        critical_columns = ["open", "high", "low", "close", "volume", "timestamp"]

        for col in critical_columns:
            with self.subTest(column=col):
                # Use parameterised column name — safe here because we
                # control the list above, not user input.
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM candles WHERE [{col}] IS NULL"
                )
                null_count = cursor.fetchone()[0]
                self.assertEqual(
                    null_count, 0,
                    f"Found {null_count} candle rows with NULL '{col}'.",
                )

        conn.close()

    # ------------------------------------------------------------------
    # 2.4  Deduplication: second run does not increase candle count
    # ------------------------------------------------------------------
    def test_04_deduplication(self):
        """Running the fetcher a second time should not insert duplicate
        candles.  We compare the count before and after a re-run."""
        conn = sqlite3.connect(DB_PATH)

        # Snapshot count before
        count_before = conn.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]
        conn.close()

        # Re-run the fetcher (import here to avoid top-level side effects)
        from data.fetcher import run_fetcher
        run_fetcher()

        # Snapshot count after
        conn = sqlite3.connect(DB_PATH)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(
            count_before,
            count_after,
            f"Candle count changed from {count_before} to {count_after} "
            "on a second run — deduplication may be broken.",
        )

    # ------------------------------------------------------------------
    # 2.5  Candle timestamps are in chronological order per symbol
    # ------------------------------------------------------------------
    def test_05_timestamps_ordered(self):
        """For each symbol, candle timestamps should be in ascending
        chronological order when ordered by timestamp."""
        conn = _connect()

        cursor = conn.execute("SELECT DISTINCT symbol FROM candles")
        symbols = [row[0] for row in cursor.fetchall()]

        for symbol in symbols:
            with self.subTest(symbol=symbol):
                cursor = conn.execute(
                    """
                    SELECT timestamp FROM candles
                    WHERE symbol = ?
                    ORDER BY timestamp ASC
                    """,
                    (symbol,),
                )
                timestamps = [row[0] for row in cursor.fetchall()]

                # Verify the list is already sorted (i.e. ascending)
                self.assertEqual(
                    timestamps,
                    sorted(timestamps),
                    f"Timestamps for '{symbol}' are not in chronological order.",
                )

                # Also verify there are no exact duplicates
                self.assertEqual(
                    len(timestamps),
                    len(set(timestamps)),
                    f"Duplicate timestamps found for '{symbol}'.",
                )

        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
#  Runner
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Use a custom test loader that preserves the declaration order so
    # Block 1 always runs before Block 2.
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = None          # keep method definition order

    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(Test_01_Database))
    suite.addTests(loader.loadTestsFromTestCase(Test_02_Fetcher))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Exit with non-zero code if any test failed
    sys.exit(0 if result.wasSuccessful() else 1)
