"""
performance_tracker.py - Recalculate and persist paper_account statistics.

Single responsibility: read closed/open trades from paper_trades, compute
every derived metric, and write them back to paper_account in one atomic
UPDATE.

Usage:
    # Programmatic (called by paper_trader after a trade closes)
    from agents.performance_tracker import update_stats
    updated_row = update_stats()

    # Standalone smoke-test
    python agents/performance_tracker.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = timezone(timedelta(hours=5, minutes=30))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _now_ist() -> datetime:
    return datetime.now(IST)


def update_stats() -> dict[str, Any]:
    """Recalculate all paper_account stats from paper_trades and persist them.

    Returns the updated paper_account row as a dict.
    """

    conn = _connect()
    try:
        columns = _table_columns(conn, "paper_account")

        # --- Fetch the account row -----------------------------------------
        row = conn.execute(
            "SELECT * FROM paper_account ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("paper_account has no rows.")
        account_id = int(row["id"])
        initial_balance = float(row["initial_balance"])

        # --- Aggregate closed-trade stats -----------------------------------
        agg = conn.execute(
            """
            SELECT
                COUNT(*)                                       AS total_trades,
                COALESCE(SUM(CASE WHEN outcome = 'WIN'  THEN 1 ELSE 0 END), 0) AS winning_trades,
                COALESCE(SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END), 0) AS losing_trades,
                COALESCE(SUM(pnl), 0)                         AS total_pnl
            FROM paper_trades
            WHERE status = 'CLOSED'
            """
        ).fetchone()

        total_trades = int(agg["total_trades"])
        winning_trades = int(agg["winning_trades"])
        losing_trades = int(agg["losing_trades"])
        total_pnl = float(agg["total_pnl"])

        # --- Daily PnL (trades whose exit_time is today IST) ----------------
        today_str = _now_ist().strftime("%Y-%m-%d")
        daily_row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS daily_pnl
            FROM paper_trades
            WHERE status = 'CLOSED'
              AND exit_time LIKE ? || '%'
            """,
            (today_str,),
        ).fetchone()
        daily_pnl = float(daily_row["daily_pnl"])

        # --- Derived metrics ------------------------------------------------
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
        balance = initial_balance + total_pnl

        # peak_balance: max of the stored peak and the current balance
        old_peak = float(row["peak_balance"]) if "peak_balance" in columns and row["peak_balance"] is not None else initial_balance
        peak_balance = max(old_peak, balance)

        # max_drawdown: max of stored drawdown and current drawdown
        if peak_balance > 0:
            current_dd = (peak_balance - balance) / peak_balance * 100
        else:
            current_dd = 0.0
        old_dd = float(row["max_drawdown"]) if "max_drawdown" in columns and row["max_drawdown"] is not None else 0.0
        max_drawdown = max(old_dd, current_dd)

        # open_positions_count
        open_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM paper_trades WHERE status = 'OPEN'"
        ).fetchone()["cnt"]

        last_updated = _now_ist().strftime("%Y-%m-%d %H:%M:%S")

        # --- Build the UPDATE dynamically based on available columns --------
        updates: list[str] = []
        values: list[Any] = []

        field_map: dict[str, Any] = {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "total_pnl": round(total_pnl, 2),
            "daily_pnl": round(daily_pnl, 2),
            "win_rate": round(win_rate, 2),
            "balance": round(balance, 2),
            "peak_balance": round(peak_balance, 2),
            "max_drawdown": round(max_drawdown, 4),
            "open_positions_count": int(open_count),
            "last_updated": last_updated,
            "updated_at": last_updated,
        }

        for col_name, col_value in field_map.items():
            if col_name in columns:
                updates.append(f"{col_name} = ?")
                values.append(col_value)

        if not updates:
            raise RuntimeError("No updatable columns found in paper_account.")

        values.append(account_id)
        conn.execute(
            f"UPDATE paper_account SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()

        # --- Return the updated row -----------------------------------------
        updated = conn.execute(
            "SELECT * FROM paper_account WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(updated)

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Standalone smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    result = update_stats()
    print("update_stats() returned:")
    print(json.dumps(result, indent=2, default=str))
