# Fix 1: Trade Replacement - Quick Reference Card

## What Changed?

### Dynamic Max Trades
```
Base: 6 trades (was 5)

TRENDING:  6 + 2 = 8 trades
SIDEWAYS:  6 + 0 = 6 trades (was 5)
VOLATILE:  6 - 2 = 4 trades
```

### Trade Replacement Criteria
System will **REPLACE** weakest trade when:
- ✓ At max capacity (no slots available)
- ✓ New signal score ≥ 20 points better
- ✓ Weakest trade losing > 0.3%
- ✓ New signal score ≥ 65

### New Exit Reason
- `REPLACED` - Trade closed to make room for better signal

## Key Log Messages

**Replacement occurred:**
```
Replaced RELIANCE (score: 45.0, PnL: -0.40%) with INFY (score: 78.0)
```

**At capacity, no replacement:**
```
AXISBANK skipped: at max capacity (6/6)
```

**Dynamic max info:**
```
Dynamic max trades: 8 (base=6, TRENDING=+2)
```

## New API Functions

### `get_open_trades_with_unrealized_pnl()`
Returns list of open trades with current unrealized PnL:
```python
[
    {
        'id': 123,
        'symbol': 'RELIANCE',
        'confidence_score': 65.0,
        'unrealized_pnl': -150.50,
        'unrealized_pnl_pct': -0.75,
        ...
    }
]
```

### `close_trade(trade_id, exit_reason, exit_price=None)`
Close a specific trade programmatically:
```python
result = close_trade(123, 'REPLACED', exit_price=1250.50)
# Returns: {'success': True, 'pnl': -150.50, ...}
```

## Database Query Examples

**Find all replaced trades:**
```sql
SELECT symbol, entry_time, exit_time, confidence_score, pnl_pct
FROM paper_trades
WHERE exit_reason = 'REPLACED'
ORDER BY exit_time DESC;
```

**Analyze replacement effectiveness:**
```sql
SELECT 
    COUNT(*) as total_replaced,
    AVG(pnl_pct) as avg_loss_at_replacement,
    AVG(confidence_score) as avg_entry_score
FROM paper_trades
WHERE exit_reason = 'REPLACED';
```

## Testing

Run Fix 1 tests:
```bash
python tests/test_fix1_trade_replacement.py
```

Expected output:
```
✓ TRENDING: base=6 + adjustment=2 = 8
✓ SIDEWAYS: base=6 + adjustment=0 = 6
✓ VOLATILE: base=6 + adjustment=-2 = 4
✅ ALL TESTS PASSED
```

## Migration

**First deployment only:**
```bash
python database/migrate_add_replaced_exit_reason.py
```

Output:
```
[SUCCESS] Migration completed successfully
  - Migrated X rows
  - Added REPLACED as valid exit_reason
```

## Monitoring Checklist

After deploying, monitor:
- [ ] Replacement frequency (check logs for "Replaced" messages)
- [ ] Replaced trades performance (would they have recovered?)
- [ ] Dynamic max trades per market condition
- [ ] Capital utilization improvement
- [ ] Overall win rate change

## Rollback (if needed)

1. Restore old orchestrator/main.py
2. Restore old agents/paper_trader.py
3. REPLACED trades remain in DB (won't break anything)

---

**Version:** 1.0  
**Date:** 2025  
**Status:** ✅ Production Ready
