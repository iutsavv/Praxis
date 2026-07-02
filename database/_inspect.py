"""Quick inspection of the live database."""
import sqlite3, json

DB = r"e:\Ai trading agent\database\trading.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("=== paper_account schema ===")
for r in conn.execute("PRAGMA table_info(paper_account)").fetchall():
    print(dict(r))

print("\n=== paper_account data ===")
row = conn.execute("SELECT * FROM paper_account ORDER BY id LIMIT 1").fetchone()
print(json.dumps(dict(row), indent=2))

print("\n=== paper_trades schema ===")
for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall():
    print(dict(r))

print("\n=== open/pending trades ===")
rows = conn.execute("SELECT * FROM paper_trades WHERE status IN ('OPEN','PENDING')").fetchall()
print(f"Count: {len(rows)}")
for r in rows:
    print(json.dumps(dict(r), indent=2))

print("\n=== closed trades count ===")
cnt = conn.execute("SELECT COUNT(*) as cnt FROM paper_trades WHERE status = 'CLOSED'").fetchone()
print(f"Closed: {cnt['cnt']}")

print("\n=== total trades in table ===")
cnt = conn.execute("SELECT COUNT(*) as cnt FROM paper_trades").fetchone()
print(f"Total rows: {cnt['cnt']}")

conn.close()
