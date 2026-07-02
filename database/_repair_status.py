"""Data repair: fix corrupted status values from the old bug."""
import sys, sqlite3
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB = r"e:\Ai trading agent\database\trading.db"
conn = sqlite3.connect(DB)

bad_statuses = "('TARGET_HIT','STOP_LOSS','EOD_EXIT')"
before = conn.execute(f"SELECT COUNT(*) FROM paper_trades WHERE status IN {bad_statuses}").fetchone()[0]
print(f"Corrupted rows before repair: {before}")

conn.execute(f"UPDATE paper_trades SET status = 'CLOSED' WHERE status IN {bad_statuses}")
conn.commit()

after = conn.execute(f"SELECT COUNT(*) FROM paper_trades WHERE status IN {bad_statuses}").fetchone()[0]
print(f"Corrupted rows after repair:  {after}")

total = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
print(f"Total rows in paper_trades:   {total}")
conn.close()
print("Done.")
