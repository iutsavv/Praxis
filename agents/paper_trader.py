"""
paper_trader.py - Paper trade execution and monitoring.

Single responsibility: execute approved paper trades and monitor open positions.
All prices are read from the local candles table. This module never makes
network calls.

Usage:
    python agents/paper_trader.py
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    from agents import risk_manager, trade_validator, performance_tracker
except ImportError:
    import risk_manager  # type: ignore
    import trade_validator  # type: ignore
    import performance_tracker  # type: ignore


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
INTERVAL = "15m"
IST = timezone(timedelta(hours=5, minutes=30))
EOD_EXIT_TIME = time(15, 15)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

PAPER_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                 TEXT NOT NULL,
    direction              TEXT NOT NULL,
    quantity               INTEGER NOT NULL DEFAULT 1,
    entry_price            REAL NOT NULL,
    entry_time             TEXT NOT NULL,
    entry_reason           TEXT,
    rsi_at_entry           REAL,
    macd_at_entry          REAL,
    vwap_at_entry          REAL,
    volume_ratio_at_entry  REAL,
    entry_rsi              REAL,
    entry_macd             REAL,
    entry_macd_signal      REAL,
    entry_macd_histogram   REAL,
    entry_ema_20           REAL,
    entry_ema_50           REAL,
    entry_vwap             REAL,
    entry_volume_ratio     REAL,
    market_condition       TEXT,
    confidence_score       REAL,
    rsi_score              REAL,
    macd_score             REAL,
    volume_score           REAL,
    vwap_score             REAL,
    target_price           REAL,
    stop_loss_price        REAL,
    capital_required       REAL NOT NULL DEFAULT 0,
    exit_price             REAL,
    exit_time              TEXT,
    exit_reason            TEXT CHECK(exit_reason IN ('TARGET_HIT', 'STOP_LOSS', 'EOD_EXIT', 'NO_CANDLE_DATA', 'REPLACED') OR exit_reason IS NULL),
    pnl                    REAL,
    pnl_pct                REAL,
    outcome                TEXT,
    status                 TEXT NOT NULL DEFAULT 'PENDING',
    no_data_count          INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_ist_text() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _paper_trades_create_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'paper_trades'"
    ).fetchone()
    return row["sql"] or "" if row else ""


def _ensure_paper_trades_schema(conn: sqlite3.Connection) -> None:
    required = {
        "symbol",
        "direction",
        "quantity",
        "entry_price",
        "entry_time",
        "entry_reason",
        "rsi_at_entry",
        "macd_at_entry",
        "vwap_at_entry",
        "volume_ratio_at_entry",
        "market_condition",
        "confidence_score",
        "target_price",
        "stop_loss_price",
        "capital_required",
        "exit_price",
        "exit_time",
        "exit_reason",
        "pnl",
        "pnl_pct",
        "outcome",
        "status",
        "no_data_count",
    }

    if not _table_exists(conn, "paper_trades"):
        conn.execute(PAPER_TRADES_DDL)
        conn.commit()
        return

    columns = _table_columns(conn, "paper_trades")
    create_sql = _paper_trades_create_sql(conn).upper()
    missing = required - columns
    status_check_blocks_pending = (
        "CHECK" in create_sql
        and "STATUS" in create_sql
        and "'PENDING'" not in create_sql
    )

    if not missing and not status_check_blocks_pending:
        return

    row_count = conn.execute("SELECT COUNT(*) AS count FROM paper_trades").fetchone()["count"]
    if row_count > 0:
        raise RuntimeError(
            "paper_trades schema is incompatible with paper_trader.py and "
            "contains existing rows. Refusing to auto-migrate trade history."
        )

    conn.execute("DROP TABLE paper_trades")
    conn.execute(PAPER_TRADES_DDL)
    conn.commit()


def _account_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM paper_account ORDER BY id LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO paper_account (balance, initial_balance) VALUES (?, ?)", (100000, 100000))
        conn.commit()
        row = conn.execute("SELECT id FROM paper_account ORDER BY id LIMIT 1").fetchone()
    return int(row["id"])


def _current_balance(conn: sqlite3.Connection) -> float:
    account_id = _account_id(conn)
    row = conn.execute(
        "SELECT balance FROM paper_account WHERE id = ?",
        (account_id,),
    ).fetchone()
    return float(row["balance"])


def _update_balance(conn: sqlite3.Connection, new_balance: float) -> None:
    account_id = _account_id(conn)
    conn.execute(
        "UPDATE paper_account SET balance = ?, updated_at = datetime('now') WHERE id = ?",
        (new_balance, account_id),
    )





# ---------------------------------------------------------------------------
# Data readers
# ---------------------------------------------------------------------------

def _latest_indicator(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT rsi, macd, macd_signal, macd_histogram, ema_20, ema_50,
               vwap, volume_ratio, timestamp
        FROM indicators
        WHERE symbol = ? AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, INTERVAL),
    ).fetchone()


def _latest_close_price(conn: sqlite3.Connection, symbol: str) -> float | None:
    candle = _latest_candle(conn, symbol)
    return float(candle["close"]) if candle else None


def _latest_candle(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT high, low, close
        FROM candles
        WHERE symbol = ? AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, INTERVAL),
    ).fetchone()


def _has_duplicate_position(conn: sqlite3.Connection, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM paper_trades
        WHERE symbol = ? AND status IN ('OPEN', 'PENDING')
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def open_trade(signal: dict[str, Any]) -> dict[str, Any]:
    """Open one approved paper trade.

    Parameters
    ----------
    signal:
        Dict with symbol, direction, weighted_score, explanation, market_condition.
    """

    trade_validator.DB_PATH = DB_PATH
    risk_manager.DB_PATH = DB_PATH

    validation = trade_validator.validate_signal(signal)
    if not validation.get("approved"):
        return {
            "success": False,
            "reason": validation.get("reason", "Signal rejected"),
            "failed_check": validation.get("failed_check"),
        }

    conn = _connect()
    try:
        _ensure_paper_trades_schema(conn)
        conn.execute("BEGIN")

        symbol = str(signal["symbol"]).upper()
        direction = str(signal["direction"]).upper()
        entry_price = _latest_close_price(conn, symbol)
        if entry_price is None:
            conn.rollback()
            return {"success": False, "reason": "No candle data"}

        position = risk_manager.calculate_position(symbol, direction, entry_price)
        capital_required = float(position["capital_required"])

        if _has_duplicate_position(conn, symbol):
            conn.rollback()
            return {"success": False, "reason": "Duplicate position"}

        balance = _current_balance(conn)
        if capital_required > balance:
            conn.rollback()
            return {"success": False, "reason": "Insufficient balance"}

        indicator = _latest_indicator(conn, symbol)
        entry_time = _now_ist_text()

        cursor = conn.execute(
            """
            INSERT INTO paper_trades (
                symbol, direction, quantity, entry_price, entry_time,
                entry_reason, rsi_at_entry, macd_at_entry, vwap_at_entry,
                volume_ratio_at_entry, entry_rsi, entry_macd, entry_macd_signal,
                entry_macd_histogram, entry_ema_20, entry_ema_50, entry_vwap,
                entry_volume_ratio, market_condition, confidence_score,
                rsi_score, macd_score, volume_score, vwap_score,
                target_price, stop_loss_price, capital_required, status
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, 'PENDING'
            )
            """,
            (
                symbol,
                direction,
                int(position["quantity"]),
                float(position["entry_price"]),
                entry_time,
                signal.get("explanation"),
                indicator["rsi"] if indicator else None,
                indicator["macd"] if indicator else None,
                indicator["vwap"] if indicator else None,
                indicator["volume_ratio"] if indicator else None,
                indicator["rsi"] if indicator else None,
                indicator["macd"] if indicator else None,
                indicator["macd_signal"] if indicator else None,
                indicator["macd_histogram"] if indicator else None,
                indicator["ema_20"] if indicator else None,
                indicator["ema_50"] if indicator else None,
                indicator["vwap"] if indicator else None,
                indicator["volume_ratio"] if indicator else None,
                signal.get("market_condition"),
                float(signal.get("weighted_score", 0)),
                signal.get("rsi_score"),
                signal.get("macd_score"),
                signal.get("volume_score"),
                signal.get("vwap_score"),
                float(position["target_price"]),
                float(position["stop_loss_price"]),
                capital_required,
            ),
        )
        trade_id = int(cursor.lastrowid)

        _update_balance(conn, balance - capital_required)
        conn.execute(
            "UPDATE paper_trades SET status = 'OPEN', updated_at = datetime('now') WHERE id = ?",
            (trade_id,),
        )
        if "open_positions_count" in _table_columns(conn, "paper_account"):
            conn.execute(
                """
                UPDATE paper_account
                SET open_positions_count = (
                    SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'
                ), last_updated = datetime('now'), updated_at = datetime('now')
                WHERE id = ?
                """,
                (_account_id(conn),),
            )
        conn.commit()
        return {"success": True, "trade_id": trade_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_open_trades_with_unrealized_pnl() -> list[dict[str, Any]]:
    """Get all open trades with current unrealized PnL.
    
    Returns list of dicts with trade details plus unrealized_pnl and unrealized_pnl_pct.
    """
    conn = _connect()
    try:
        _ensure_paper_trades_schema(conn)
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY id ASC"
        ).fetchall()
        
        result = []
        for trade in trades:
            candle = _latest_candle(conn, trade["symbol"])
            if candle is None:
                # Skip trades with no current data
                continue
                
            current_price = float(candle["close"])
            direction = str(trade["direction"]).upper()
            entry_price = float(trade["entry_price"])
            quantity = int(trade["quantity"])
            
            unrealized_pnl = _calculate_pnl(direction, entry_price, current_price, quantity)
            capital_required = float(trade["capital_required"] or 0)
            unrealized_pnl_pct = (unrealized_pnl / capital_required) * 100 if capital_required else 0.0
            
            result.append({
                "id": trade["id"],
                "symbol": trade["symbol"],
                "direction": direction,
                "quantity": quantity,
                "entry_price": entry_price,
                "confidence_score": float(trade["confidence_score"] or 0),
                "capital_required": capital_required,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
            })
        
        return result
    finally:
        conn.close()


def close_trade(trade_id: int, exit_reason: str, exit_price: float | None = None) -> dict[str, Any]:
    """Close a specific trade by ID.
    
    Parameters
    ----------
    trade_id : int
        The ID of the trade to close
    exit_reason : str
        Reason for exit: 'REPLACED', 'TARGET_HIT', 'STOP_LOSS', 'EOD_EXIT', etc.
    exit_price : float | None
        Exit price. If None, uses current market price.
        
    Returns
    -------
    dict
        Result with success status and trade details
    """
    conn = _connect()
    try:
        _ensure_paper_trades_schema(conn)
        conn.execute("BEGIN")
        
        trade = conn.execute(
            "SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,)
        ).fetchone()
        
        if trade is None:
            conn.rollback()
            return {"success": False, "reason": "Trade not found or already closed"}
        
        # Get current price if not provided
        if exit_price is None:
            candle = _latest_candle(conn, trade["symbol"])
            if candle is None:
                conn.rollback()
                return {"success": False, "reason": "No current price data"}
            exit_price = float(candle["close"])
        
        direction = str(trade["direction"]).upper()
        entry_price = float(trade["entry_price"])
        quantity = int(trade["quantity"])
        
        pnl = _calculate_pnl(direction, entry_price, exit_price, quantity)
        capital_required = float(trade["capital_required"] or 0)
        pnl_pct = (pnl / capital_required) * 100 if capital_required else 0.0
        outcome = "WIN" if pnl > 0 else "LOSS"
        
        new_balance = _current_balance(conn) + capital_required + pnl
        exit_time = _now_ist_text()
        
        _update_balance(conn, new_balance)
        conn.execute(
            """
            UPDATE paper_trades
            SET exit_price = ?,
                exit_time = ?,
                exit_reason = ?,
                pnl = ?,
                pnl_pct = ?,
                outcome = ?,
                status = 'CLOSED',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (exit_price, exit_time, exit_reason, pnl, pnl_pct, outcome, trade_id),
        )
        
        if "open_positions_count" in _table_columns(conn, "paper_account"):
            conn.execute(
                """
                UPDATE paper_account
                SET open_positions_count = (
                    SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'
                ), last_updated = datetime('now'), updated_at = datetime('now')
                WHERE id = ?
                """,
                (_account_id(conn),),
            )
        
        conn.commit()
        
        return {
            "success": True,
            "trade_id": trade_id,
            "symbol": trade["symbol"],
            "exit_reason": exit_reason,
            "outcome": outcome,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def monitor_open_trades() -> list[dict[str, Any]]:
    """Monitor all open paper trades and close/cancel positions when needed."""

    conn = _connect()
    closed: list[dict[str, Any]] = []
    try:
        _ensure_paper_trades_schema(conn)
        trades = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY id ASC"
        ).fetchall()

        for trade in trades:
            candle = _latest_candle(conn, trade["symbol"])
            exit_reason: str | None = None
            outcome: str | None = None
            exit_price: float | None = None

            if candle is None:
                no_data_count = int(trade["no_data_count"] or 0) + 1
                if no_data_count >= 3:
                    conn.execute(
                        """
                        UPDATE paper_trades
                        SET status = 'CANCELLED',
                            exit_reason = 'NO_CANDLE_DATA',
                            no_data_count = ?,
                            updated_at = datetime('now')
                        WHERE id = ?
                        """,
                        (no_data_count, trade["id"]),
                    )
                    closed.append(
                        {
                            "trade_id": trade["id"],
                            "symbol": trade["symbol"],
                            "status": "CANCELLED",
                            "exit_reason": "NO_CANDLE_DATA",
                            "outcome": None,
                        }
                    )
                else:
                    conn.execute(
                        "UPDATE paper_trades SET no_data_count = ?, updated_at = datetime('now') WHERE id = ?",
                        (no_data_count, trade["id"]),
                    )
                conn.commit()
                continue

            direction = str(trade["direction"]).upper()
            target_price = float(trade["target_price"])
            stop_loss_price = float(trade["stop_loss_price"])
            current_price = float(candle["close"])
            high_price = float(candle["high"])
            low_price = float(candle["low"])

            if (
                (direction == "BUY" and high_price >= target_price)
                or (direction == "SELL" and low_price <= target_price)
            ):
                exit_reason = "TARGET_HIT"
                outcome = "WIN"
                exit_price = target_price
            elif (
                (direction == "BUY" and low_price <= stop_loss_price)
                or (direction == "SELL" and high_price >= stop_loss_price)
            ):
                exit_reason = "STOP_LOSS"
                outcome = "LOSS"
                exit_price = stop_loss_price
            elif datetime.now(IST).time() >= EOD_EXIT_TIME:
                exit_reason = "EOD_EXIT"
                pnl_preview = _calculate_pnl(direction, float(trade["entry_price"]), current_price, int(trade["quantity"]))
                outcome = "WIN" if pnl_preview > 0 else "LOSS"
                exit_price = current_price

            if exit_reason is None:
                if int(trade["no_data_count"] or 0) != 0:
                    conn.execute(
                        "UPDATE paper_trades SET no_data_count = 0, updated_at = datetime('now') WHERE id = ?",
                        (trade["id"],),
                    )
                    conn.commit()
                continue

            pnl = _calculate_pnl(
                direction,
                float(trade["entry_price"]),
                float(exit_price),
                int(trade["quantity"]),
            )
            capital_required = float(trade["capital_required"] or 0)
            pnl_pct = (pnl / capital_required) * 100 if capital_required else 0.0
            new_balance = _current_balance(conn) + capital_required + pnl
            exit_time = _now_ist_text()

            _update_balance(conn, new_balance)
            conn.execute(
                """
                UPDATE paper_trades
                SET exit_price = ?,
                    exit_time = ?,
                    exit_reason = ?,
                    pnl = ?,
                    pnl_pct = ?,
                    outcome = ?,
                    status = ?,
                    no_data_count = 0,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    float(exit_price),
                    exit_time,
                    exit_reason,
                    pnl,
                    pnl_pct,
                    outcome,
                    "CLOSED",
                    trade["id"],
                ),
            )
            conn.commit()

            closed.append(
                {
                    "trade_id": trade["id"],
                    "symbol": trade["symbol"],
                    "status": "CLOSED",
                    "exit_reason": exit_reason,
                    "outcome": outcome,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                }
            )

        if closed:
            performance_tracker.update_stats()

        return closed
    finally:
        conn.close()


def _calculate_pnl(direction: str, entry_price: float, exit_price: float, quantity: int) -> float:
    if direction == "BUY":
        return (exit_price - entry_price) * quantity
    return (entry_price - exit_price) * quantity


# ---------------------------------------------------------------------------
# Standalone test harness
# ---------------------------------------------------------------------------

def _create_test_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE paper_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                balance REAL DEFAULT 100000,
                initial_balance REAL DEFAULT 100000,
                total_pnl REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE strategy_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                min_score_to_trade REAL NOT NULL DEFAULT 60,
                max_open_trades INTEGER NOT NULL DEFAULT 3,
                risk_per_trade_pct REAL NOT NULL DEFAULT 1.0,
                trade_in_sideways INTEGER NOT NULL DEFAULT 1
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

            CREATE TABLE candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE indicators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candle_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                rsi REAL,
                macd REAL,
                macd_signal REAL,
                macd_histogram REAL,
                ema_20 REAL,
                ema_50 REAL,
                vwap REAL,
                volume_ratio REAL,
                timestamp TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute("INSERT INTO paper_account (balance, initial_balance) VALUES (?, ?)", (100000, 100000))
        conn.execute(
            """
            INSERT INTO strategy_rules (
                version, is_active, min_score_to_trade, max_open_trades,
                risk_per_trade_pct, trade_in_sideways
            ) VALUES (1, 1, 60, 3, 1.0, 1)
            """
        )
        conn.execute(PAPER_TRADES_DDL)
        conn.commit()
    finally:
        conn.close()


def _insert_test_market_data(
    path: Path,
    symbol: str,
    close: float,
    timestamp: str,
    high: float | None = None,
    low: float | None = None,
) -> None:
    conn = sqlite3.connect(path)
    try:
        high = close if high is None else high
        low = close if low is None else low
        cursor = conn.execute(
            """
            INSERT INTO candles (symbol, interval, open, high, low, close, volume, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, INTERVAL, close, high, low, close, 1000, timestamp),
        )
        candle_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO indicators (
                candle_id, symbol, interval, rsi, macd, macd_signal,
                macd_histogram, ema_20, ema_50, vwap, volume_ratio, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (candle_id, symbol, INTERVAL, 55, 1.2, 0.8, 0.4, close, close, close, 1.5, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def _test_open_and_target(path: Path) -> bool:
    _insert_test_market_data(path, "TESTSTOCK", 500, "2026-01-01 09:15:00")
    result = open_trade(
        {
            "symbol": "TESTSTOCK",
            "direction": "BUY",
            "weighted_score": 75,
            "explanation": "test target setup",
            "market_condition": "TRENDING",
        },
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        trade = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (result.get("trade_id"),)).fetchone()
        balance_after_open = conn.execute("SELECT balance FROM paper_account WHERE id = 1").fetchone()["balance"]
        opened_ok = bool(
            result["success"]
            and trade["status"] == "OPEN"
            and trade["quantity"] == 40
            and balance_after_open == 80000
        )
    finally:
        conn.close()

    _insert_test_market_data(path, "TESTSTOCK", 506, "2026-01-01 09:30:00", high=508, low=505)
    closed = monitor_open_trades()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        trade = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (result["trade_id"],)).fetchone()
        balance_after_close = conn.execute("SELECT balance FROM paper_account WHERE id = 1").fetchone()["balance"]
        return (
            opened_ok
            and len(closed) == 1
            and trade["status"] == "CLOSED"
            and trade["exit_reason"] == "TARGET_HIT"
            and trade["outcome"] == "WIN"
            and round(trade["exit_price"], 2) == 507.50
            and round(trade["pnl"], 2) == 300
            and round(balance_after_close, 2) == 100300
        )
    finally:
        conn.close()


def _test_stop_loss(path: Path) -> bool:
    _insert_test_market_data(path, "TESTSTOCK", 500, "2026-01-02 09:15:00")
    result = open_trade(
        {
            "symbol": "TESTSTOCK",
            "direction": "BUY",
            "weighted_score": 75,
            "explanation": "test stop setup",
            "market_condition": "TRENDING",
        },
    )
    _insert_test_market_data(path, "TESTSTOCK", 497, "2026-01-02 09:30:00", high=498, low=495)
    closed = monitor_open_trades()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        trade = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (result["trade_id"],)).fetchone()
        balance_after_close = conn.execute("SELECT balance FROM paper_account WHERE id = 1").fetchone()["balance"]
        return (
            result["success"]
            and len(closed) == 1
            and trade["status"] == "CLOSED"
            and trade["exit_reason"] == "STOP_LOSS"
            and trade["outcome"] == "LOSS"
            and round(trade["exit_price"], 2) == 496.25
            and round(trade["pnl"], 2) == -150
            and round(balance_after_close, 2) == 99850
        )
    finally:
        conn.close()


def _run_standalone_tests() -> None:
    global DB_PATH

    test_db = PROJECT_ROOT / "agents" / "_paper_trader_test.db"
    original_db_path = DB_PATH
    original_validator_now = trade_validator._TEST_NOW

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{test_db}{suffix}")
        if candidate.exists():
            candidate.unlink()

    try:
        DB_PATH = test_db
        trade_validator._TEST_NOW = trade_validator.IST.localize(datetime(2026, 1, 5, 10, 0))

        _create_test_database(test_db)
        target_passed = _test_open_and_target(test_db)
        print(f"TARGET_HIT / WIN scenario: {'PASS' if target_passed else 'FAIL'}")

        test_db.unlink()
        _create_test_database(test_db)
        stop_passed = _test_stop_loss(test_db)
        print(f"STOP_LOSS / LOSS scenario: {'PASS' if stop_passed else 'FAIL'}")
    finally:
        DB_PATH = original_db_path
        trade_validator._TEST_NOW = original_validator_now
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{test_db}{suffix}")
            if candidate.exists():
                candidate.unlink()


if __name__ == "__main__":
    _run_standalone_tests()
