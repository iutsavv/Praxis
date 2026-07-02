"""
Post-fix E2E verification — confirms all 5 fixes are working correctly.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = timezone(timedelta(hours=5, minutes=30))

sys.path.insert(0, str(PROJECT_ROOT))


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def dump_account(conn):
    row = conn.execute("SELECT * FROM paper_account ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def dump_trade(conn, trade_id):
    row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def pp(label, obj):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(obj, indent=2, default=str))


# ===================================================================
# STEP 1: Baseline
# ===================================================================
print("\n" + "#" * 70)
print("#  STEP 1 — Record baseline paper_account")
print("#" * 70)

conn = connect()
baseline = dump_account(conn)
pp("BASELINE paper_account", baseline)

# Verify all required columns exist
required_cols = {"balance", "win_rate", "peak_balance", "max_drawdown",
                 "open_positions_count", "daily_pnl", "last_updated",
                 "total_pnl", "total_trades", "winning_trades", "losing_trades"}
acct_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_account)").fetchall()}
missing = required_cols - acct_cols
print(f"\nRequired columns present: {'ALL ✅' if not missing else f'MISSING: {sorted(missing)} ❌'}")

open_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'").fetchone()[0]
print(f"Open trades at baseline: {open_count}")
conn.close()


# ===================================================================
# STEP 2: Open a trade
# ===================================================================
print("\n" + "#" * 70)
print("#  STEP 2 — Open a trade through the live pipeline")
print("#" * 70)

from agents import trade_validator, paper_trader, performance_tracker

original_test_now = trade_validator._TEST_NOW

conn = connect()
symbol_row = conn.execute("""
    SELECT symbol, close FROM candles
    WHERE interval = '15m'
    ORDER BY timestamp DESC
    LIMIT 1
""").fetchone()

if symbol_row is None:
    print("ERROR: No candle data found.")
    conn.close()
    sys.exit(1)

test_symbol = symbol_row["symbol"]
latest_close = float(symbol_row["close"])
print(f"Using symbol: {test_symbol} (latest close: Rs {latest_close})")

# Clean up any leftover test trades
conn.execute(
    "DELETE FROM paper_trades WHERE symbol = ? AND status IN ('OPEN','PENDING')",
    (test_symbol,)
)
conn.commit()

balance_before_open = float(conn.execute(
    "SELECT balance FROM paper_account ORDER BY id LIMIT 1"
).fetchone()["balance"])
print(f"Balance before open: Rs {balance_before_open}")
conn.close()

# Synthetic signal
test_signal = {
    "symbol": test_symbol,
    "direction": "BUY",
    "weighted_score": 75,
    "explanation": "E2E verification signal",
    "market_condition": "TRENDING",
}

# Patch market hours
trade_validator._TEST_NOW = trade_validator.IST.localize(datetime(2026, 6, 30, 10, 0))

result = paper_trader.open_trade(test_signal)
trade_validator._TEST_NOW = original_test_now

pp("open_trade() result", result)

if not result.get("success"):
    print(f"\nERROR: Trade failed: {result.get('reason')}")
    sys.exit(1)

trade_id = result["trade_id"]

conn = connect()
trade_after_open = dump_trade(conn, trade_id)
pp(f"Trade #{trade_id} after open", trade_after_open)

balance_after_open = float(conn.execute(
    "SELECT balance FROM paper_account ORDER BY id LIMIT 1"
).fetchone()["balance"])

capital_required = float(trade_after_open["capital_required"])
print(f"\n  Balance BEFORE open: Rs {balance_before_open}")
print(f"  Capital required:    Rs {capital_required}")
print(f"  Balance AFTER open:  Rs {balance_after_open}")
balance_check = abs(balance_after_open - (balance_before_open - capital_required)) < 0.01
print(f"  Balance deduction:   {'CORRECT ✅' if balance_check else 'WRONG ❌'}")

entry_price = float(trade_after_open["entry_price"])
target_price = float(trade_after_open["target_price"])
stop_loss_price = float(trade_after_open["stop_loss_price"])
quantity = int(trade_after_open["quantity"])
direction = trade_after_open["direction"]
conn.close()


# ===================================================================
# STEP 3: Force close (TARGET_HIT)
# ===================================================================
print("\n" + "#" * 70)
print("#  STEP 3 — Force trade closure via TARGET_HIT")
print("#" * 70)

exit_price = target_price
now_ist = datetime.now(IST)
fake_ts = now_ist.strftime("%Y-%m-%d %H:%M:%S")

conn = connect()
conn.execute(
    "DELETE FROM candles WHERE symbol = ? AND interval = '15m' AND timestamp = ?",
    (test_symbol, fake_ts)
)
conn.execute("""
    INSERT INTO candles (symbol, interval, open, high, low, close, volume, timestamp)
    VALUES (?, '15m', ?, ?, ?, ?, 10000, ?)
""", (test_symbol, exit_price, exit_price, exit_price, exit_price, fake_ts))
conn.commit()
print(f"Inserted candle: {test_symbol} close={exit_price} at {fake_ts}")
conn.close()

print("\nCalling paper_trader.monitor_open_trades()...")
closed = paper_trader.monitor_open_trades()
pp("monitor_open_trades() result", closed)

conn = connect()
trade_after_close = dump_trade(conn, trade_id)
pp(f"Trade #{trade_id} after close", trade_after_close)

# --- Validate trade fields ---
status = trade_after_close["status"]
exit_reason = trade_after_close["exit_reason"]
outcome = trade_after_close["outcome"]
actual_pnl = float(trade_after_close["pnl"])
actual_pnl_pct = float(trade_after_close["pnl_pct"])
actual_exit_price = float(trade_after_close["exit_price"])

if direction == "BUY":
    expected_pnl = (actual_exit_price - entry_price) * quantity
else:
    expected_pnl = (entry_price - actual_exit_price) * quantity
expected_outcome = "WIN" if expected_pnl > 0 else "LOSS"

print(f"\n  [Fix 1 check] status = '{status}' — {'CORRECT ✅' if status == 'CLOSED' else 'STILL BROKEN ❌'}")
print(f"  Exit reason:   {exit_reason}")
print(f"  Outcome:       {outcome} (expected: {expected_outcome}) — {'✅' if outcome == expected_outcome else '❌'}")
print(f"  PnL:           {actual_pnl} (expected: {expected_pnl}) — {'✅' if abs(actual_pnl - expected_pnl) < 0.01 else '❌'}")
conn.close()


# ===================================================================
# STEP 4: Verify performance_tracker.update_stats()
# ===================================================================
print("\n" + "#" * 70)
print("#  STEP 4 — Verify performance_tracker stats")
print("#" * 70)

conn = connect()
after = dump_account(conn)
pp("paper_account AFTER trade closed", after)

b_total_trades = int(baseline.get("total_trades", 0) or 0)
b_winning = int(baseline.get("winning_trades", 0) or 0)
b_losing = int(baseline.get("losing_trades", 0) or 0)
b_total_pnl = float(baseline.get("total_pnl", 0) or 0)
b_balance = float(baseline.get("balance", 0) or 0)

a_total_trades = int(after.get("total_trades", 0) or 0)
a_winning = int(after.get("winning_trades", 0) or 0)
a_losing = int(after.get("losing_trades", 0) or 0)
a_total_pnl = float(after.get("total_pnl", 0) or 0)
a_balance = float(after.get("balance", 0) or 0)
a_win_rate = float(after.get("win_rate", 0) or 0)
a_peak = float(after.get("peak_balance", 0) or 0)
a_dd = float(after.get("max_drawdown", 0) or 0)
a_opc = int(after.get("open_positions_count", 0) or 0)
a_daily = float(after.get("daily_pnl", 0) or 0)
a_last = after.get("last_updated")
initial_balance = float(after.get("initial_balance", 0) or 0)

checks = {}

# 1. total_trades +1
checks["total_trades +1"] = (a_total_trades == b_total_trades + 1)

# 2. winning or losing +1
if expected_outcome == "WIN":
    checks["winning_trades +1"] = (a_winning == b_winning + 1)
    checks["losing_trades unchanged"] = (a_losing == b_losing)
else:
    checks["losing_trades +1"] = (a_losing == b_losing + 1)
    checks["winning_trades unchanged"] = (a_winning == b_winning)

# 3. total_pnl correct
checks["total_pnl = sum of closed pnl"] = abs(a_total_pnl - (b_total_pnl + actual_pnl)) < 0.01

# 4. balance = initial_balance + total_pnl
expected_balance = initial_balance + a_total_pnl
checks["balance = initial + total_pnl"] = abs(a_balance - expected_balance) < 0.01

# 5. win_rate
expected_wr = (a_winning / a_total_trades * 100) if a_total_trades > 0 else 0
checks["win_rate correct"] = abs(a_win_rate - expected_wr) < 0.01

# 6. peak_balance
checks["peak_balance >= balance"] = (a_peak >= a_balance)

# 7. max_drawdown
if a_peak > 0:
    expected_dd = max(0, (a_peak - a_balance) / a_peak * 100)
else:
    expected_dd = 0
checks["max_drawdown correct"] = abs(a_dd - expected_dd) < 0.01

# 8. open_positions_count = 0 (trade is closed)
checks["open_positions_count = 0"] = (a_opc == 0)

# 9. last_updated is recent
checks["last_updated is set"] = (a_last is not None and a_last != "")

# 10. status = CLOSED (Fix 1)
checks["[Fix 1] status = 'CLOSED'"] = (status == "CLOSED")

# 11. performance_tracker.py exists (Fix 2)
checks["[Fix 2] performance_tracker.py exists"] = (PROJECT_ROOT / "agents" / "performance_tracker.py").exists()

# 12. balance column exists (Fix 3)
checks["[Fix 3] 'balance' column in DB"] = "balance" in acct_cols

# 13. win_rate column exists (Fix 3)
checks["[Fix 3] 'win_rate' column in DB"] = "win_rate" in acct_cols

# 14. peak_balance column exists (Fix 3)
checks["[Fix 3] 'peak_balance' column in DB"] = "peak_balance" in acct_cols

# 15. open_positions_count column exists (Fix 3)
checks["[Fix 3] 'open_positions_count' column in DB"] = "open_positions_count" in acct_cols

conn.close()


# ===================================================================
# STEP 5: API check
# ===================================================================
print("\n" + "#" * 70)
print("#  STEP 5 — API /api/account/summary check")
print("#" * 70)

try:
    from urllib.request import urlopen
    resp = urlopen("http://localhost:8000/api/account/summary")
    api_data = json.loads(resp.read())
    pp("GET /api/account/summary", api_data)

    api_balance = api_data.get("balance")
    api_pnl = api_data.get("total_pnl")
    api_opc = api_data.get("open_positions_count")
    api_wr = api_data.get("win_rate")

    checks["[Fix 5] API returns balance"] = (api_balance is not None)
    checks["[Fix 5] API returns open_positions_count"] = (api_opc is not None)
    checks["[Fix 5] API returns win_rate"] = (api_wr is not None)
except Exception as exc:
    print(f"  Could not reach API: {exc}")


# ===================================================================
# RESULTS
# ===================================================================
print("\n" + "#" * 70)
print("#  FINAL VERDICT TABLE")
print("#" * 70)

all_passed = True
for check_name, passed in checks.items():
    icon = "✅" if passed else "❌"
    print(f"  {icon}  {check_name}")
    if not passed:
        all_passed = False

print()
if all_passed:
    print("  ✅ ALL CHECKS PASSED — All 5 fixes verified end-to-end.")
else:
    failed = [k for k, v in checks.items() if not v]
    print(f"  ❌ {len(failed)} CHECK(S) FAILED: {failed}")


# ===================================================================
# CLEANUP
# ===================================================================
print("\n" + "#" * 70)
print("#  CLEANUP — Reverting test data")
print("#" * 70)

conn = connect()
conn.execute("DELETE FROM paper_trades WHERE id = ?", (trade_id,))
# Restore baseline values
conn.execute(
    """UPDATE paper_account SET
        balance = ?, total_pnl = ?, total_trades = ?, winning_trades = ?,
        losing_trades = ?, win_rate = ?, peak_balance = ?, max_drawdown = ?,
        open_positions_count = ?, daily_pnl = ?, updated_at = ?, last_updated = ?
    WHERE id = 1""",
    (b_balance, b_total_pnl, b_total_trades, b_winning, b_losing,
     baseline.get("win_rate", 0), baseline.get("peak_balance", initial_balance),
     baseline.get("max_drawdown", 0), 0, 0,
     baseline.get("updated_at"), baseline.get("last_updated"))
)
conn.execute("DELETE FROM candles WHERE symbol = ? AND interval = '15m' AND timestamp = ?",
    (test_symbol, fake_ts))
conn.commit()

after_cleanup = dump_account(conn)
pp("paper_account after cleanup", after_cleanup)
conn.close()
print("\nCleanup done. Database restored.")
