"""
trade_validator.py - Signal approval checks for the AI Paper Trading Agent.

Single responsibility: decide whether a signal is allowed to become a trade.
This module does not calculate position size and does not execute trades.

Usage:
    python agents/trade_validator.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 15)

_TEST_NOW: datetime | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_ist() -> datetime:
    if _TEST_NOW is not None:
        if _TEST_NOW.tzinfo is None:
            return IST.localize(_TEST_NOW)
        return _TEST_NOW.astimezone(IST)
    return datetime.now(IST)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _get_balance(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT balance FROM paper_account ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return 0.0
    return float(row["balance"])


def _get_strategy_rules(conn: sqlite3.Connection) -> dict[str, Any]:
    columns = _table_columns(conn, "strategy_rules")
    wanted = ["min_score_to_trade", "max_open_trades"]
    if "trade_in_sideways" in columns:
        wanted.append("trade_in_sideways")

    row = conn.execute(
        f"""
        SELECT {', '.join(wanted)}
        FROM strategy_rules
        WHERE is_active = 1
        ORDER BY version DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No active strategy_rules row found.")

    rules = dict(row)
    rules.setdefault("trade_in_sideways", 1)
    return rules


def _is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False

    current = (now.hour, now.minute)
    return MARKET_OPEN <= current <= MARKET_CLOSE


def _log_trade_decision(
    conn: sqlite3.Connection,
    signal: dict[str, Any],
    approved: bool,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO trade_logs (timestamp, symbol, direction, score, approved, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            str(signal.get("symbol", "")).upper(),
            str(signal.get("direction", "")),
            float(signal.get("weighted_score", 0)),
            1 if approved else 0,
            reason,
        ),
    )
    conn.commit()


def _reject(
    conn: sqlite3.Connection,
    signal: dict[str, Any],
    reason: str,
    failed_check: str,
) -> dict[str, Any]:
    _log_trade_decision(conn, signal, False, reason)
    return {"approved": False, "reason": reason, "failed_check": failed_check}


def validate_signal(signal: dict) -> dict:
    """Approve or reject a signal for trading."""

    conn = _connect()
    try:
        rules = _get_strategy_rules(conn)
        symbol = str(signal.get("symbol", "")).upper()
        direction = str(signal.get("direction", "")).upper()
        weighted_score = float(signal.get("weighted_score", 0))
        market_condition = str(signal.get("market_condition", "")).upper()

        if not _is_market_hours(_now_ist()):
            return _reject(conn, signal, "Market is closed", "market_hours")

        if weighted_score < float(rules["min_score_to_trade"]):
            return _reject(conn, signal, "Score below threshold", "score_threshold")

        if direction == "HOLD":
            return _reject(conn, signal, "Direction is HOLD", "direction")

        duplicate = conn.execute(
            """
            SELECT 1
            FROM paper_trades
            WHERE symbol = ? AND status IN ('OPEN', 'PENDING')
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if duplicate is not None:
            return _reject(conn, signal, "Duplicate position", "duplicate_position")

        open_count = conn.execute(
            "SELECT COUNT(*) AS count FROM paper_trades WHERE status = 'OPEN'"
        ).fetchone()["count"]
        if int(open_count) >= int(rules["max_open_trades"]):
            return _reject(conn, signal, "Max open trades reached", "max_open_trades")

        if _get_balance(conn) < 5000:
            return _reject(conn, signal, "Insufficient capital", "capital_check")

        if market_condition == "SIDEWAYS" and int(rules.get("trade_in_sideways", 1)) == 0:
            return _reject(conn, signal, "Sideways market trades disabled", "sideways_filter")

        _log_trade_decision(conn, signal, True, "Approved")
        return {"approved": True}
    finally:
        conn.close()


def _create_test_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE strategy_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                min_score_to_trade REAL NOT NULL DEFAULT 60,
                max_open_trades INTEGER NOT NULL DEFAULT 3,
                trade_in_sideways INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE paper_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                balance REAL DEFAULT 100000,
                initial_balance REAL DEFAULT 100000
            );

            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                score REAL,
                approved INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO strategy_rules (
                version, is_active, min_score_to_trade, max_open_trades,
                trade_in_sideways
            ) VALUES (1, 1, 60, 3, 1)
            """
        )
        conn.execute("INSERT INTO paper_account (balance, initial_balance) VALUES (?, ?)", (100000, 100000))
        conn.commit()
    finally:
        conn.close()


def _reset_test_state(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM trade_logs")
        conn.execute("UPDATE paper_account SET balance = 100000")
        conn.execute(
            """
            UPDATE strategy_rules
            SET min_score_to_trade = 60,
                max_open_trades = 3,
                trade_in_sideways = 1
            WHERE version = 1
            """
        )
        conn.commit()
    finally:
        conn.close()


def _base_signal(**overrides: Any) -> dict[str, Any]:
    signal = {
        "symbol": "TESTSTOCK",
        "direction": "BUY",
        "weighted_score": 75,
        "explanation": "test signal",
        "market_condition": "TRENDING",
    }
    signal.update(overrides)
    return signal


def _run_case(path: Path, name: str, setup: Any, expected: dict[str, Any]) -> bool:
    _reset_test_state(path)
    setup()
    result = validate_signal(expected.pop("signal", _base_signal()))
    passed = all(result.get(key) == value for key, value in expected.items())
    print(f"{name}: {'PASS' if passed else 'FAIL'}")
    if not passed:
        print(f"  result={result}")
    return passed


def _run_standalone_tests() -> None:
    global DB_PATH, _TEST_NOW

    test_db = PROJECT_ROOT / "agents" / "_trade_validator_test.db"
    original_db_path = DB_PATH
    original_now = _TEST_NOW
    if test_db.exists():
        test_db.unlink()

    try:
        DB_PATH = test_db
        _create_test_database(test_db)
        _TEST_NOW = IST.localize(datetime(2026, 1, 5, 10, 0))

        def no_setup() -> None:
            pass

        def set_closed_time() -> None:
            global _TEST_NOW
            _TEST_NOW = IST.localize(datetime(2026, 1, 5, 8, 59))

        def set_low_balance() -> None:
            conn = sqlite3.connect(test_db)
            conn.execute("UPDATE paper_account SET balance = 4999")
            conn.commit()
            conn.close()

        def set_duplicate() -> None:
            conn = sqlite3.connect(test_db)
            conn.execute("INSERT INTO paper_trades (symbol, status) VALUES ('TESTSTOCK', 'OPEN')")
            conn.commit()
            conn.close()

        def set_max_open() -> None:
            conn = sqlite3.connect(test_db)
            for i in range(3):
                conn.execute("INSERT INTO paper_trades (symbol, status) VALUES (?, 'OPEN')", (f"OPEN{i}",))
            conn.commit()
            conn.close()

        def disable_sideways() -> None:
            conn = sqlite3.connect(test_db)
            conn.execute("UPDATE strategy_rules SET trade_in_sideways = 0")
            conn.commit()
            conn.close()

        cases = [
            ("market hours rejection", set_closed_time, {"approved": False, "failed_check": "market_hours"}),
            (
                "score threshold rejection",
                no_setup,
                {"approved": False, "failed_check": "score_threshold", "signal": _base_signal(weighted_score=59)},
            ),
            (
                "direction rejection",
                no_setup,
                {"approved": False, "failed_check": "direction", "signal": _base_signal(direction="HOLD")},
            ),
            ("duplicate rejection", set_duplicate, {"approved": False, "failed_check": "duplicate_position"}),
            ("max open trades rejection", set_max_open, {"approved": False, "failed_check": "max_open_trades"}),
            ("capital rejection", set_low_balance, {"approved": False, "failed_check": "capital_check"}),
            (
                "sideways rejection",
                disable_sideways,
                {"approved": False, "failed_check": "sideways_filter", "signal": _base_signal(market_condition="SIDEWAYS")},
            ),
            ("success approval", no_setup, {"approved": True}),
        ]

        for name, setup, expected in cases:
            _TEST_NOW = IST.localize(datetime(2026, 1, 5, 10, 0))
            _run_case(test_db, name, setup, dict(expected))
    finally:
        DB_PATH = original_db_path
        _TEST_NOW = original_now
        if test_db.exists():
            test_db.unlink()


if __name__ == "__main__":
    _run_standalone_tests()
