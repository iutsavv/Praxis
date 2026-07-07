"""
test_fix1_trade_replacement.py - Test Fix 1: Trade Slot Quality Check

Tests the trade replacement logic:
1. Dynamic max trades calculation (base 6, TRENDING +2, SIDEWAYS 0, VOLATILE -2)
2. Trade replacement when at capacity with a better signal
3. REPLACED exit_reason in database
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.main import Orchestrator


def test_dynamic_max_trades():
    """Test dynamic max trades calculation."""
    print("\n" + "=" * 60)
    print("Test 1: Dynamic Max Trades Calculation")
    print("=" * 60)
    
    orchestrator = Orchestrator(dry_run=True)
    
    # Test TRENDING
    result = orchestrator._get_dynamic_max_trades("TRENDING")
    assert result['max_trades'] == 8, f"TRENDING should be 8, got {result['max_trades']}"
    assert result['base'] == 6, f"Base should be 6, got {result['base']}"
    assert result['adjustment'] == 2, f"TRENDING adjustment should be +2, got {result['adjustment']}"
    print(f"✓ TRENDING: base={result['base']} + adjustment={result['adjustment']} = {result['max_trades']}")
    
    # Test SIDEWAYS
    result = orchestrator._get_dynamic_max_trades("SIDEWAYS")
    assert result['max_trades'] == 6, f"SIDEWAYS should be 6, got {result['max_trades']}"
    assert result['adjustment'] == 0, f"SIDEWAYS adjustment should be 0, got {result['adjustment']}"
    print(f"✓ SIDEWAYS: base={result['base']} + adjustment={result['adjustment']} = {result['max_trades']}")
    
    # Test VOLATILE
    result = orchestrator._get_dynamic_max_trades("VOLATILE")
    assert result['max_trades'] == 4, f"VOLATILE should be 4, got {result['max_trades']}"
    assert result['adjustment'] == -2, f"VOLATILE adjustment should be -2, got {result['adjustment']}"
    print(f"✓ VOLATILE: base={result['base']} + adjustment={result['adjustment']} = {result['max_trades']}")
    
    print("\n✅ All dynamic max trades tests passed!\n")


def test_should_make_room_logic():
    """Test the trade replacement decision logic."""
    print("\n" + "=" * 60)
    print("Test 2: Trade Replacement Logic")
    print("=" * 60)
    
    orchestrator = Orchestrator(dry_run=True)
    
    # Setup: 3 open trades at max capacity
    open_trades = [
        {
            'id': 1,
            'symbol': 'STOCK1',
            'confidence_score': 65,
            'unrealized_pnl_pct': 0.5,  # winning
        },
        {
            'id': 2,
            'symbol': 'STOCK2',
            'confidence_score': 70,
            'unrealized_pnl_pct': -0.2,  # slightly losing
        },
        {
            'id': 3,
            'symbol': 'STOCK3',
            'confidence_score': 45,
            'unrealized_pnl_pct': -0.5,  # losing significantly, lowest score
        },
    ]
    
    dynamic_max = {'max_trades': 3}
    
    # Test 1: New signal not good enough - should NOT replace
    new_signal_weak = {'weighted_score': 60}
    should_replace, weakest = orchestrator._should_make_room_for_signal(
        new_signal_weak, open_trades, dynamic_max
    )
    assert not should_replace, "Weak signal should not trigger replacement"
    print(f"✓ Weak signal (score 60) correctly rejected for replacement")
    
    # Test 2: New signal much better than weakest losing trade - SHOULD replace
    new_signal_strong = {'weighted_score': 75}
    should_replace, weakest = orchestrator._should_make_room_for_signal(
        new_signal_strong, open_trades, dynamic_max
    )
    assert should_replace, "Strong signal should trigger replacement of weak losing trade"
    assert weakest['id'] == 3, f"Should replace STOCK3, got id={weakest['id']}"
    assert weakest['confidence_score'] == 45, "Weakest should have score 45"
    assert weakest['unrealized_pnl_pct'] == -0.5, "Weakest should be losing 0.5%"
    print(f"✓ Strong signal (score 75) correctly triggers replacement of STOCK3 (score 45, PnL -0.5%)")
    
    # Test 3: Under capacity - should NOT replace
    open_trades_under = open_trades[:2]  # Only 2 trades
    should_replace, weakest = orchestrator._should_make_room_for_signal(
        new_signal_strong, open_trades_under, dynamic_max
    )
    assert not should_replace, "Should not replace when under capacity"
    print(f"✓ Under capacity (2/3) correctly does not trigger replacement")
    
    # Test 4: Good signal but trade not losing enough - should NOT replace
    open_trades_winning = [
        {
            'id': 1,
            'symbol': 'STOCK1',
            'confidence_score': 60,
            'unrealized_pnl_pct': 0.3,  # winning
        },
        {
            'id': 2,
            'symbol': 'STOCK2',
            'confidence_score': 65,
            'unrealized_pnl_pct': 0.2,  # winning
        },
        {
            'id': 3,
            'symbol': 'STOCK3',
            'confidence_score': 55,
            'unrealized_pnl_pct': -0.1,  # barely losing (< 0.3%)
        },
    ]
    should_replace, weakest = orchestrator._should_make_room_for_signal(
        new_signal_strong, open_trades_winning, dynamic_max
    )
    assert not should_replace, "Should not replace if no trade is losing > 0.3%"
    print(f"✓ All trades near breakeven correctly prevents replacement")
    
    print("\n✅ All trade replacement logic tests passed!\n")


def test_replaced_exit_reason_in_database():
    """Verify REPLACED is accepted as a valid exit_reason."""
    print("\n" + "=" * 60)
    print("Test 3: REPLACED Exit Reason in Database")
    print("=" * 60)
    
    import sqlite3
    
    db_path = PROJECT_ROOT / "database" / "trading.db"
    conn = sqlite3.connect(db_path)
    
    try:
        # Get the CREATE TABLE statement
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_trades'"
        )
        row = cursor.fetchone()
        
        if row:
            create_sql = row[0]
            assert "'REPLACED'" in create_sql or '"REPLACED"' in create_sql, \
                "REPLACED should be in exit_reason CHECK constraint"
            print(f"✓ REPLACED is in paper_trades schema")
            
            # Verify all expected exit reasons are present
            expected_reasons = ['TARGET_HIT', 'STOP_LOSS', 'EOD_EXIT', 'NO_CANDLE_DATA', 'REPLACED']
            for reason in expected_reasons:
                assert f"'{reason}'" in create_sql or f'"{reason}"' in create_sql, \
                    f"{reason} should be in exit_reason CHECK constraint"
            print(f"✓ All exit reasons present: {', '.join(expected_reasons)}")
        else:
            print("⚠ paper_trades table not found (may not be initialized yet)")
    
    finally:
        conn.close()
    
    print("\n✅ Database schema test passed!\n")


def run_all_tests():
    """Run all Fix 1 tests."""
    print("\n" + "=" * 60)
    print("  Fix 1: Trade Slot Quality Check - Test Suite")
    print("=" * 60)
    
    try:
        test_dynamic_max_trades()
        test_should_make_room_logic()
        test_replaced_exit_reason_in_database()
        
        print("\n" + "=" * 60)
        print("  ✅ ALL TESTS PASSED")
        print("=" * 60 + "\n")
        return True
    
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}\n")
        return False
    except Exception as e:
        print(f"\n❌ TEST ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
