"""
Test script for the two-stage scanning pipeline.
Verifies the complete workflow: Stage 1 → Stage 2 → Results
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from analysis.stage1_scanner import run_stage1_scan
from analysis.stage2_scanner import run_stage2_scan
import sqlite3

DB_PATH = PROJECT_ROOT / "database" / "trading.db"


def test_pipeline():
    print("\n" + "=" * 70)
    print("  TWO-STAGE SCANNING PIPELINE TEST")
    print("=" * 70 + "\n")
    
    # Step 1: Run Stage 1
    print("STEP 1: Running Stage 1 broad scan...")
    print("-" * 70)
    flagged_symbols, scan_time = run_stage1_scan()
    
    print(f"\n✓ Stage 1 completed successfully")
    print(f"  Flagged {len(flagged_symbols)} stocks for deep analysis")
    print(f"  Scan time: {scan_time}\n")
    
    # Step 2: Run Stage 2 (on a subset for testing)
    print("STEP 2: Running Stage 2 deep analysis...")
    print("-" * 70)
    
    # Test with a small subset to avoid long execution time
    test_subset = flagged_symbols[:10] if len(flagged_symbols) > 10 else flagged_symbols
    print(f"  Testing with {len(test_subset)} stocks (subset of {len(flagged_symbols)})\n")
    
    candidates = run_stage2_scan(test_subset, scan_time)
    
    print(f"\n✓ Stage 2 completed successfully")
    print(f"  Found {len(candidates)} candidates above threshold\n")
    
    # Step 3: Verify database writes
    print("STEP 3: Verifying database records...")
    print("-" * 70)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check Stage 1 records
    stage1_count = cursor.execute(
        "SELECT COUNT(*) FROM scan_results WHERE stage = 1 AND scan_time = (SELECT MAX(scan_time) FROM scan_results)"
    ).fetchone()[0]
    
    # Check Stage 2 records
    stage2_count = cursor.execute(
        "SELECT COUNT(*) FROM scan_results WHERE stage = 2 AND scan_time = (SELECT MAX(scan_time) FROM scan_results)"
    ).fetchone()[0]
    
    # Check selected candidates
    selected_count = cursor.execute(
        "SELECT COUNT(*) FROM scan_results WHERE final_selected = 1 AND scan_time = (SELECT MAX(scan_time) FROM scan_results)"
    ).fetchone()[0]
    
    conn.close()
    
    print(f"  Stage 1 records in DB: {stage1_count}")
    print(f"  Stage 2 records in DB: {stage2_count}")
    print(f"  Candidates selected: {selected_count}\n")
    
    # Step 4: Display sample results
    if candidates:
        print("STEP 4: Sample candidates")
        print("-" * 70)
        print(f"  {'#':<4} {'Symbol':<12} {'Direction':<10} {'Score':<8} {'Reason'}")
        print("  " + "-" * 66)
        
        for i, cand in enumerate(candidates[:5], 1):
            print(f"  {i:<4} {cand['symbol']:<12} {cand['direction']:<10} "
                  f"{cand['score']:<8.1f} "
                  f"RSI={cand['rsi_score']:.0f} MACD={cand['macd_score']:.0f} "
                  f"VOL={cand['volume_score']:.0f}")
    
    # Final summary
    print("\n" + "=" * 70)
    print("  TEST SUMMARY")
    print("=" * 70)
    print(f"  ✓ Stage 1: {len(flagged_symbols)} stocks flagged")
    print(f"  ✓ Stage 2: {len(test_subset)} stocks analyzed")
    print(f"  ✓ Candidates: {len(candidates)} above threshold")
    print(f"  ✓ Database: {stage1_count} Stage 1 + {stage2_count} Stage 2 records")
    print("=" * 70 + "\n")
    
    return {
        "stage1_flagged": len(flagged_symbols),
        "stage2_analyzed": len(test_subset),
        "candidates": len(candidates),
        "db_stage1": stage1_count,
        "db_stage2": stage2_count,
    }


if __name__ == "__main__":
    try:
        results = test_pipeline()
        
        # Validate results
        if results["stage1_flagged"] > 0 and results["db_stage1"] > 0:
            print("✅ TWO-STAGE PIPELINE TEST PASSED\n")
            sys.exit(0)
        else:
            print("❌ TWO-STAGE PIPELINE TEST FAILED\n")
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ TEST FAILED WITH ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
