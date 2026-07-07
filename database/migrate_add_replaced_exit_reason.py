"""
migrate_add_replaced_exit_reason.py - Add REPLACED as valid exit_reason

This migration updates the paper_trades table to include 'REPLACED' as a valid
exit_reason in the CHECK constraint.

Run directly:
    python database/migrate_add_replaced_exit_reason.py
"""

import sqlite3
import os
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent
DB_PATH = DB_DIR / "trading.db"


def migrate() -> None:
    """Update paper_trades table to support REPLACED exit_reason."""
    
    if not DB_PATH.exists():
        print(f"[ERROR] Database not found at {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Check if paper_trades table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trades'"
        )
        if not cursor.fetchone():
            print("[INFO] paper_trades table does not exist yet - no migration needed")
            return
        
        # Get the current CREATE TABLE statement
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_trades'"
        )
        row = cursor.fetchone()
        if not row:
            print("[ERROR] Could not retrieve table schema")
            return
        
        create_sql = row['sql']
        
        # Check if REPLACED is already in the constraint
        if "'REPLACED'" in create_sql or '"REPLACED"' in create_sql:
            print("[INFO] REPLACED exit_reason already exists - no migration needed")
            return
        
        print("[INFO] Starting migration to add REPLACED exit_reason...")
        
        # Begin transaction
        cursor.execute("BEGIN")
        
        # Create new table with updated constraint
        cursor.execute("""
            CREATE TABLE paper_trades_new (
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
        """)
        
        # Copy data from old table to new table (explicit column list to handle schema changes)
        cursor.execute("""
            INSERT INTO paper_trades_new (
                id, symbol, direction, quantity, entry_price, entry_time, entry_reason,
                rsi_at_entry, macd_at_entry, vwap_at_entry, volume_ratio_at_entry,
                entry_rsi, entry_macd, entry_macd_signal, entry_macd_histogram,
                entry_ema_20, entry_ema_50, entry_vwap, entry_volume_ratio,
                market_condition, confidence_score, rsi_score, macd_score, volume_score, vwap_score,
                target_price, stop_loss_price, capital_required,
                exit_price, exit_time, exit_reason, pnl, pnl_pct, outcome, status, no_data_count,
                created_at, updated_at
            )
            SELECT 
                id, symbol, direction, quantity, entry_price, entry_time, entry_reason,
                rsi_at_entry, macd_at_entry, vwap_at_entry, volume_ratio_at_entry,
                entry_rsi, entry_macd, entry_macd_signal, entry_macd_histogram,
                entry_ema_20, entry_ema_50, entry_vwap, entry_volume_ratio,
                market_condition, confidence_score, rsi_score, macd_score, volume_score, vwap_score,
                target_price, stop_loss_price, capital_required,
                exit_price, exit_time, exit_reason, pnl, pnl_pct, outcome, status, 
                COALESCE(no_data_count, 0), created_at, updated_at
            FROM paper_trades
        """)
        
        # Get row count to verify
        cursor.execute("SELECT COUNT(*) FROM paper_trades")
        old_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM paper_trades_new")
        new_count = cursor.fetchone()[0]
        
        if old_count != new_count:
            print(f"[ERROR] Row count mismatch: old={old_count}, new={new_count}")
            cursor.execute("ROLLBACK")
            return
        
        # Drop old table and rename new table
        cursor.execute("DROP TABLE paper_trades")
        cursor.execute("ALTER TABLE paper_trades_new RENAME TO paper_trades")
        
        # Commit transaction
        conn.commit()
        
        print(f"[SUCCESS] Migration completed successfully")
        print(f"  - Migrated {new_count} rows")
        print(f"  - Added REPLACED as valid exit_reason")
        
    except Exception as e:
        print(f"[ERROR] Migration failed: {e}")
        cursor.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Migration: Add REPLACED exit_reason")
    print("=" * 60 + "\n")
    migrate()
    print("\n" + "=" * 60)
    print("  Migration complete")
    print("=" * 60 + "\n")
