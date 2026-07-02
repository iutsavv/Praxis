"""
risk_manager.py - Position sizing for the AI Paper Trading Agent.

Single responsibility: calculate position size and price levels.
Reads paper_account and strategy_rules only. Does not write to the database.

Usage:
    python agents/risk_manager.py
"""

from __future__ import annotations

import math
import logging
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
MAX_CAPITAL_PER_TRADE_PCT = 0.20
SENSIBILITY_CAPITAL_PCT = 0.25
STOP_LOSS_DISTANCE_PCT = 0.0075

logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_balance(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT balance FROM paper_account ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("paper_account has no account row.")
    return float(row["balance"])


def _get_risk_per_trade_pct(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT risk_per_trade_pct
        FROM strategy_rules
        WHERE is_active = 1
        ORDER BY version DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No active strategy_rules row found.")
    return float(row["risk_per_trade_pct"])


def calculate_position(symbol: str, direction: str, entry_price: float) -> dict:
    """Calculate position size and risk levels for a proposed trade."""

    direction = direction.upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")

    conn = _connect()
    try:
        balance = _get_balance(conn)
        risk_per_trade_pct = _get_risk_per_trade_pct(conn)
    finally:
        conn.close()

    max_loss_amount = balance * (risk_per_trade_pct / 100)
    if direction == "BUY":
        stop_loss_price = entry_price * (1 - STOP_LOSS_DISTANCE_PCT)
        target_price = entry_price * 1.015
    else:
        stop_loss_price = entry_price * (1 + STOP_LOSS_DISTANCE_PCT)
        target_price = entry_price * 0.985

    risk_per_share = abs(entry_price - stop_loss_price)
    quantity_from_risk = math.floor(max_loss_amount / risk_per_share)
    max_capital_per_trade = balance * MAX_CAPITAL_PER_TRADE_PCT
    max_quantity_from_capital = math.floor(max_capital_per_trade / entry_price)
    max_quantity = max_quantity_from_capital
    quantity = min(quantity_from_risk, max_quantity)
    quantity = max(quantity, 0)
    capital_required = quantity * entry_price
    capital_constrained = quantity < quantity_from_risk

    if capital_required > balance * SENSIBILITY_CAPITAL_PCT:
        logger.warning(
            "%s %s position capital %.2f exceeded %.0f%% of balance %.2f; "
            "reducing to %.0f%% cap",
            direction,
            symbol.upper(),
            capital_required,
            SENSIBILITY_CAPITAL_PCT * 100,
            balance,
            MAX_CAPITAL_PER_TRADE_PCT * 100,
        )
        quantity = max_quantity
        capital_required = quantity * entry_price
        capital_constrained = True

    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "entry_price": float(entry_price),
        "stop_loss_price": float(stop_loss_price),
        "target_price": float(target_price),
        "quantity": int(quantity),
        "risk_per_share": float(risk_per_share),
        "max_loss_amount": float(max_loss_amount),
        "quantity_from_risk": int(quantity_from_risk),
        "max_capital_per_trade": float(max_capital_per_trade),
        "max_quantity_from_capital": int(max_quantity_from_capital),
        "max_quantity": int(max_quantity),
        "capital_required": float(capital_required),
        "capital_constrained": capital_constrained,
    }


def _create_test_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE paper_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                balance REAL DEFAULT 100000,
                initial_balance REAL DEFAULT 100000
            );

            CREATE TABLE strategy_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                risk_per_trade_pct REAL NOT NULL DEFAULT 1.0
            );
            """
        )
        conn.execute("INSERT INTO paper_account (balance, initial_balance) VALUES (?, ?)", (100000, 100000))
        conn.execute(
            "INSERT INTO strategy_rules (version, is_active, risk_per_trade_pct) VALUES (1, 1, 1.0)"
        )
        conn.commit()
    finally:
        conn.close()


def _run_standalone_tests() -> None:
    global DB_PATH

    test_db = PROJECT_ROOT / "agents" / "_risk_manager_test.db"
    original_db_path = DB_PATH
    if test_db.exists():
        test_db.unlink()

    try:
        DB_PATH = test_db
        _create_test_database(test_db)

        scenarios = [
            ("RELIANCE", "BUY", 2800),
            ("INFY", "BUY", 1032.40),
            ("TATAMOTORS", "SELL", 975),
        ]
        results = [calculate_position(symbol, direction, price) for symbol, direction, price in scenarios]
        for result in results:
            print(
                f"{result['symbol']} {result['direction']} @ Rs {result['entry_price']:.2f} | "
                f"balance=Rs 100000.00 | risk=1.00% | "
                f"max_loss=Rs {result['max_loss_amount']:.2f} | "
                f"stop_loss=Rs {result['stop_loss_price']:.2f} | "
                f"risk_per_share=Rs {result['risk_per_share']:.2f} | "
                f"quantity_from_risk={result['quantity_from_risk']} | "
                f"max_capital=Rs {result['max_capital_per_trade']:.2f} | "
                f"max_quantity_from_capital={result['max_quantity_from_capital']} | "
                f"final_quantity={result['quantity']} | "
                f"capital_required=Rs {result['capital_required']:.2f}"
            )

        expected = (
            results[0]["max_loss_amount"] == 1000
            and round(results[0]["stop_loss_price"], 2) == 2779.00
            and round(results[0]["risk_per_share"], 2) == 21.00
            and results[0]["quantity_from_risk"] == 47
            and results[0]["max_quantity_from_capital"] == 7
            and results[0]["quantity"] == 7
            and results[0]["capital_required"] == 19600
            and all(
                result["capital_required"] <= result["max_capital_per_trade"]
                for result in results
            )
        )
        print(f"20 percent capital cap sizing scenarios: {'PASS' if expected else 'FAIL'}")
    finally:
        DB_PATH = original_db_path
        if test_db.exists():
            test_db.unlink()


if __name__ == "__main__":
    _run_standalone_tests()
