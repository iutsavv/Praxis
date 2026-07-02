"""
migrate_paper_account.py — One-time migration to add missing columns.

Checks current columns via PRAGMA, adds any that are missing from the
canonical schema, and copies values from legacy column names.

Safe to run multiple times — it only ADDs columns that don't exist yet.
"""

import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parent / "trading.db"

# Canonical columns that MUST exist, with their type and default
REQUIRED_COLUMNS: list[tuple[str, str, str]] = [
    # (column_name, type, default_value_sql)
    ("balance",              "REAL",    "100000"),
    ("daily_pnl",            "REAL",    "0"),
    ("win_rate",             "REAL",    "0"),
    ("max_drawdown",         "REAL",    "0"),
    ("peak_balance",         "REAL",    "100000"),
    ("open_positions_count", "INTEGER", "0"),
    ("last_updated",         "TEXT",    "NULL"),
]


def get_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(paper_account)").fetchall()}


def migrate() -> None:
    conn = sqlite3.connect(DB_PATH)

    before = sorted(get_columns(conn))
    print(f"Columns BEFORE migration ({len(before)}):")
    for col in before:
        print(f"  • {col}")

    added: list[str] = []
    existing = get_columns(conn)

    for col_name, col_type, default_val in REQUIRED_COLUMNS:
        if col_name not in existing:
            sql = f"ALTER TABLE paper_account ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"
            print(f"\n  Running: {sql}")
            conn.execute(sql)
            added.append(col_name)
        else:
            print(f"\n  ✅ {col_name} already exists — skipping")

    # Copy values from legacy columns to canonical ones if needed
    current_cols = get_columns(conn)

    if "current_balance" in current_cols and "balance" in current_cols:
        print("\n  Copying current_balance → balance ...")
        conn.execute("UPDATE paper_account SET balance = current_balance WHERE balance IS NULL OR balance = 100000")

    if "updated_at" in current_cols and "last_updated" in current_cols:
        print("  Copying updated_at → last_updated ...")
        conn.execute("UPDATE paper_account SET last_updated = updated_at WHERE last_updated IS NULL")

    # Set peak_balance = initial_balance if it's still at default
    if "peak_balance" in current_cols and "initial_balance" in current_cols:
        conn.execute("UPDATE paper_account SET peak_balance = initial_balance WHERE peak_balance = 100000 AND initial_balance != 100000")

    conn.commit()

    after = sorted(get_columns(conn))
    print(f"\nColumns AFTER migration ({len(after)}):")
    for col in after:
        marker = " ← NEW" if col in added else ""
        print(f"  • {col}{marker}")

    if added:
        print(f"\n✅ Added {len(added)} column(s): {', '.join(added)}")
    else:
        print("\n✅ All columns already present — nothing to add.")

    # Show the actual row data
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM paper_account ORDER BY id LIMIT 1").fetchone()
    if row:
        import json
        print(f"\nCurrent paper_account row:")
        print(json.dumps(dict(row), indent=2, default=str))

    conn.close()


if __name__ == "__main__":
    migrate()
