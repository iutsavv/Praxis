"""
stage2_scanner.py - Deep analysis of Stage 1 flagged stocks.

Single responsibility: Perform detailed technical analysis on stocks flagged
by Stage 1 scanner. Uses existing signal_engine for scoring, plus pattern
detection and sentiment analysis (stubs for future features).

Works with data already in database - no additional network calls needed.
"""

import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from analysis.signal_engine import (
    get_strategy_weights,
    score_stock,
    detect_market_condition,
    load_config,
)

DB_PATH = PROJECT_ROOT / "database" / "trading.db"


def get_connection() -> sqlite3.Connection:
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def check_recent_candles(symbol: str, minutes: int = 10) -> bool:
    """Check if symbol has candle data from last N minutes."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cutoff = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    row = cursor.execute("""
        SELECT COUNT(*) FROM candles
        WHERE symbol = ?
        AND timestamp >= datetime(?, '-' || ? || ' minutes')
    """, (symbol, cutoff, minutes)).fetchone()
    
    conn.close()
    return row[0] > 0 if row else False


def detect_pattern(symbol: str) -> str | None:
    """Stub for pattern detection (Feature 9).
    
    Future: Implement candlestick pattern recognition
    (doji, hammer, engulfing, etc.)
    """
    # TODO: Implement pattern detection
    return None


def check_sentiment(symbol: str) -> dict[str, Any] | None:
    """Stub for news sentiment analysis (Feature 7).
    
    Future: Integrate news API and sentiment scoring
    """
    # TODO: Implement sentiment analysis
    return None


def write_stage2_results(
    results: list[dict[str, Any]], 
    scan_time: str
) -> int:
    """Write Stage 2 results to scan_results table."""
    conn = get_connection()
    cursor = conn.cursor()
    
    updated = 0
    for result in results:
        try:
            # Update existing Stage 1 record with Stage 2 data
            cursor.execute("""
                UPDATE scan_results
                SET stage = 2,
                    stage2_score = ?,
                    stage2_direction = ?,
                    stage2_pattern = ?,
                    stage2_setup = ?,
                    final_selected = ?
                WHERE symbol = ? AND scan_time = ?
            """, (
                result['score'],
                result['direction'],
                result.get('pattern'),
                result.get('setup'),
                result.get('final_selected', 0),
                result['symbol'],
                scan_time
            ))
            
            if cursor.rowcount > 0:
                updated += 1
            else:
                # If no Stage 1 record exists, insert new Stage 2 record
                cursor.execute("""
                    INSERT OR REPLACE INTO scan_results (
                        symbol, scan_time, stage,
                        stage2_score, stage2_direction,
                        stage2_pattern, stage2_setup, final_selected
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    result['symbol'],
                    scan_time,
                    2,  # Stage 2
                    result['score'],
                    result['direction'],
                    result.get('pattern'),
                    result.get('setup'),
                    result.get('final_selected', 0)
                ))
                updated += 1
                
        except sqlite3.Error as e:
            print(f"    [ERROR] Failed to write {result['symbol']}: {e}")
    
    conn.commit()
    conn.close()
    return updated


def run_stage2_scan(symbols: list[str], scan_time: str = "") -> list[dict[str, Any]]:
    """Main Stage 2 scanner function.
    
    Parameters:
        symbols: List of symbols flagged by Stage 1
        scan_time: Scan timestamp from Stage 1 (ensures records link correctly).
                   If empty, generates a new timestamp (standalone usage).
        
    Returns:
        List of top candidate dicts with scoring and analysis
    """
    print("\n" + "=" * 64)
    print("  STAGE 2 SCANNER - Deep Technical Analysis")
    print("=" * 64)
    
    scan_started = time.time()
    if not scan_time:
        scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if not symbols:
        print("  No symbols to analyze.")
        print("=" * 64 + "\n")
        return []
    
    print(f"  Analyzing {len(symbols)} flagged stocks...")
    
    conn = get_connection()
    cfg = load_config()
    weights = get_strategy_weights(conn)
    market_condition = detect_market_condition(conn, cfg)
    
    print(f"  Market condition: {market_condition}")
    print(f"  Min score threshold: {weights.min_score_to_trade}")
    
    results: list[dict[str, Any]] = []
    analyzed = 0
    skipped = 0
    
    for symbol in symbols:
        try:
            # Check if we have recent candle and indicator data
            if not check_recent_candles(symbol, minutes=30):
                print(f"  [SKIP] {symbol:<12} - no recent candle data")
                skipped += 1
                continue
            
            # Score using existing signal_engine
            signal_result = score_stock(conn, symbol, weights, market_condition, cfg)
            
            if signal_result is None:
                print(f"  [SKIP] {symbol:<12} - no indicator data")
                skipped += 1
                continue
            
            # Detect patterns (stub)
            pattern = detect_pattern(symbol)
            
            # Check sentiment (stub)
            sentiment = check_sentiment(symbol)
            
            # Build result
            result = {
                'symbol': symbol,
                'score': signal_result.weighted_score,
                'direction': signal_result.direction,
                'rsi_score': signal_result.rsi_score,
                'macd_score': signal_result.macd_score,
                'volume_score': signal_result.volume_score,
                'vwap_score': signal_result.vwap_score,
                'market_condition': signal_result.market_condition,
                'explanation': signal_result.explanation,
                'pattern': pattern,
                'setup': sentiment.get('setup') if sentiment else None,
                'final_selected': 1 if (
                    signal_result.direction != 'HOLD' and
                    signal_result.weighted_score >= weights.min_score_to_trade
                ) else 0,
            }
            
            results.append(result)
            analyzed += 1
            
            status = "✓" if result['final_selected'] else "○"
            print(f"  [{status}] {symbol:<12} - {result['direction']:<5} "
                  f"score={result['score']:.1f}")
            
        except Exception as e:
            print(f"  [ERROR] {symbol:<12} - {e}")
            skipped += 1
    
    conn.close()
    
    # Write results to database
    write_start = time.time()
    updated = write_stage2_results(results, scan_time)
    write_duration = time.time() - write_start
    
    # Filter and sort top candidates
    top_picks = [r for r in results if r['final_selected'] == 1]
    top_picks.sort(key=lambda x: x['score'], reverse=True)
    
    total_duration = time.time() - scan_started
    
    print("-" * 64)
    print(f"  STAGE 2 COMPLETE")
    print(f"  Analyzed: {analyzed} stocks in {total_duration:.1f}s")
    print(f"  Skipped: {skipped} (no data)")
    print(f"  Candidates: {len(top_picks)} above threshold")
    print(f"  DB updates: {updated} records")
    print("=" * 64 + "\n")
    
    return top_picks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test with some symbols (standalone mode generates its own scan_time)
    test_symbols = ["RELIANCE", "INFY", "HDFCBANK", "TCS", "ICICIBANK"]
    
    print("  [TEST MODE] Running Stage 2 on sample symbols")
    candidates = run_stage2_scan(test_symbols)
    
    if candidates:
        print(f"\n  TOP CANDIDATES ({len(candidates)}):")
        for i, cand in enumerate(candidates[:5], 1):
            print(f"    {i}. {cand['symbol']:<12} - {cand['direction']:<5} "
                  f"score={cand['score']:.1f}")
    else:
        print("\n  No candidates above threshold.")
