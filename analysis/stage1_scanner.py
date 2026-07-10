"""
stage1_scanner.py - Fast broad scan of all NSE stocks.

Single responsibility: Quickly scan all 2700+ NSE stocks and flag candidates
for deep analysis based on price movement, volume, F&O status, and open trades.

No indicator calculations. Minimal network calls.
Target: Complete in under 30 seconds for all stocks.
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"

# Stage 1 thresholds
PRICE_CHANGE_THRESHOLD = 0.5  # Flag if abs(change) > 0.5%
VOLUME_RATIO_THRESHOLD = 1.5  # Flag if volume > 1.5x average
PRICE_CHECK_INTERVAL_MINUTES = 15  # Compare with price from 15 min ago

# NSE headers for API calls
NSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.nseindia.com',
}


def get_session() -> requests.Session:
    """Create session with retry strategy."""
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(NSE_HEADERS)
    return session


def get_all_prices_nse() -> pd.DataFrame:
    """Fetch current price and volume for ALL NSE stocks via bhav copy CSV.

    Tries the NSE bhav copy first (one CSV download for all stocks).
    Falls back to the F&O securities API, then to local DB data.

    Returns DataFrame: symbol, current_price, today_volume, prev_close
    """
    # Try NSE bhav copy CSV for today (or most recent trading day)
    session = get_session()

    # First visit NSE homepage to get cookies (required by NSE)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception:
        pass  # Cookies may still work without this

    # Try bhav copy for last few days (in case today's isn't available yet)
    for days_back in range(0, 4):
        target_date = datetime.now() - timedelta(days=days_back)
        if target_date.weekday() >= 5:  # Skip weekends
            continue

        date_str = target_date.strftime("%d%m%Y")
        url = (
            f"https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{date_str}.csv"
        )

        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200 and len(resp.text) > 500:
                df = pd.read_csv(StringIO(resp.text.strip()))

                # Normalise column names (bhav copy has spaces)
                df.columns = df.columns.str.strip()

                # Filter for EQ series (equity)
                if 'SERIES' in df.columns:
                    df = df[df['SERIES'].str.strip().isin(['EQ', 'BE'])]

                result = pd.DataFrame({
                    'symbol': df['SYMBOL'].str.strip(),
                    'current_price': pd.to_numeric(df['CLOSE_PRICE'], errors='coerce'),
                    'prev_close': pd.to_numeric(df['PREV_CLOSE'], errors='coerce'),
                    'today_volume': pd.to_numeric(
                        df.get('TTL_TRD_QNTY', df.get('TOTAL_TRADE_QUANTITY', 0)),
                        errors='coerce',
                    ),
                }).dropna(subset=['current_price', 'prev_close'])

                if len(result) > 100:
                    print(f"  NSE bhav copy loaded: {len(result)} stocks ({target_date.strftime('%Y-%m-%d')})")
                    return result
        except Exception as e:
            print(f"  [WARN] Bhav copy fetch failed for {date_str}: {e}")

    # Fallback: try the F&O securities API (covers ~200 F&O stocks)
    try:
        fo_url = "https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"
        resp = session.get(fo_url, timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                rows = []
                for item in data:
                    symbol = item.get("symbol", "").strip()
                    if not symbol or symbol == "NIFTY 50":
                        continue
                    rows.append({
                        'symbol': symbol,
                        'current_price': float(item.get("lastPrice", 0)),
                        'prev_close': float(item.get("previousClose", 0)),
                        'today_volume': int(item.get("totalTradedVolume", 0)),
                    })
                if rows:
                    df = pd.DataFrame(rows)
                    print(f"  NSE F&O API loaded: {len(df)} stocks")
                    return df
    except Exception as e:
        print(f"  [WARN] NSE F&O API failed: {e}")

    # Final fallback: local database
    print("  [WARN] NSE APIs unavailable, falling back to local DB data")
    return get_all_prices_from_db()


def get_all_prices_from_db() -> pd.DataFrame:
    """Get latest prices from candles table - fastest method (DB fallback).

    Returns DataFrame with columns: symbol, current_price, prev_close, today_volume
    """
    conn = sqlite3.connect(DB_PATH)

    # Get latest candle for each symbol
    query = """
        SELECT 
            symbol,
            close as current_price,
            volume as today_volume,
            timestamp
        FROM candles 
        WHERE (symbol, timestamp) IN (
            SELECT symbol, MAX(timestamp)
            FROM candles
            GROUP BY symbol
        )
    """

    df = pd.read_sql_query(query, conn)

    # Get previous close (candle before latest)
    prev_query = """
        WITH ranked AS (
            SELECT 
                symbol,
                close,
                timestamp,
                ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) as rn
            FROM candles
        )
        SELECT symbol, close as prev_close
        FROM ranked
        WHERE rn = 2
    """

    prev_df = pd.read_sql_query(prev_query, conn)
    conn.close()

    # Merge
    if not prev_df.empty:
        df = df.merge(prev_df, on='symbol', how='left')
    else:
        df['prev_close'] = df['current_price']

    return df


def calculate_price_change(symbol: str, current_price: float, prev_close: float) -> float:
    """Calculate percentage price change.

    Parameters
    ----------
    symbol : str
        Stock symbol (unused in calculation, kept for spec compliance and logging).
    current_price : float
        Current trading price.
    prev_close : float
        Previous close price.

    Returns
    -------
    float
        Percentage change: (current - prev) / prev × 100
    """
    if prev_close == 0 or prev_close is None:
        return 0.0
    return ((current_price - prev_close) / prev_close) * 100


def get_average_volume(symbol: str) -> float:
    """Get average daily volume from stock_universe (cached) or calculate from candles.

    Uses the cached avg_daily_volume from stock_universe first (fast).
    Falls back to computing AVG(volume) from the last 10 *trading days*
    of daily-aggregated candle data.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Try stock_universe first (faster)
    row = cursor.execute(
        "SELECT avg_daily_volume FROM stock_universe WHERE symbol = ? AND is_active = 1",
        (symbol,)
    ).fetchone()

    if row and row[0] and row[0] > 0:
        conn.close()
        return float(row[0])

    # Fallback: aggregate candles by date and average the last 10 trading days
    row = cursor.execute(
        """SELECT AVG(daily_volume) FROM (
            SELECT DATE(timestamp) as trade_date, SUM(volume) as daily_volume
            FROM candles
            WHERE symbol = ?
            GROUP BY DATE(timestamp)
            ORDER BY trade_date DESC
            LIMIT 10
        )""",
        (symbol,)
    ).fetchone()

    conn.close()
    return float(row[0]) if row and row[0] else 100000.0  # Default


def get_fo_stocks() -> set[str]:
    """Get set of F&O eligible symbols."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    rows = cursor.execute(
        "SELECT symbol FROM stock_universe WHERE is_fo_stock = 1 AND is_active = 1"
    ).fetchall()

    conn.close()
    return {row[0] for row in rows}


def get_open_trade_symbols() -> set[str]:
    """Get symbols of currently open trades."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    rows = cursor.execute(
        "SELECT DISTINCT symbol FROM paper_trades WHERE status = 'OPEN'"
    ).fetchall()

    conn.close()
    return {row[0] for row in rows}


def get_all_active_symbols() -> list[str]:
    """Get all active symbols from stock_universe."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    rows = cursor.execute(
        "SELECT symbol FROM stock_universe WHERE is_active = 1 ORDER BY symbol"
    ).fetchall()

    conn.close()
    return [row[0] for row in rows]


def write_stage1_results(flagged_stocks: list[dict[str, Any]], scan_time: str) -> int:
    """Write Stage 1 results to scan_results table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    inserted = 0
    for stock in flagged_stocks:
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO scan_results (
                    symbol, scan_time, stage,
                    stage1_flagged, stage1_price_change, 
                    stage1_volume_ratio, stage1_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                stock['symbol'],
                scan_time,
                1,  # Stage 1
                1,  # Flagged
                stock.get('price_change', 0.0),
                stock.get('volume_ratio', 0.0),
                stock.get('reason', '')
            ))
            inserted += 1
        except sqlite3.Error as e:
            print(f"    [ERROR] Failed to write {stock['symbol']}: {e}")

    conn.commit()
    conn.close()
    return inserted


def run_stage1_scan() -> tuple[list[str], str]:
    """Main Stage 1 scanner function.

    Returns:
        Tuple of (flagged symbol names, scan_time string) for Stage 2 to use.
    """
    print("\n" + "=" * 64)
    print("  STAGE 1 SCANNER - Broad Market Scan")
    print("=" * 64)

    scan_started = time.time()
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Get F&O stocks and open trades (always flag these)
    fo_stocks = get_fo_stocks()
    open_trade_symbols = get_open_trade_symbols()

    print(f"  F&O stocks: {len(fo_stocks)}")
    print(f"  Open trades: {len(open_trade_symbols)}")

    # Fetch prices — try live NSE first, fall back to DB
    print("  Fetching prices...")
    price_fetch_start = time.time()

    try:
        prices_df = get_all_prices_nse()
        price_fetch_duration = time.time() - price_fetch_start
        print(f"  Price fetch completed in {price_fetch_duration:.1f}s")
    except Exception as e:
        print(f"  [ERROR] Failed to fetch prices: {e}")
        return [], scan_time

    total_stocks = len(prices_df)
    print(f"  Total stocks to scan: {total_stocks:,}")

    # Scan and flag stocks
    flagged_stocks: list[dict[str, Any]] = []
    processing_start = time.time()

    for _, row in prices_df.iterrows():
        symbol = row['symbol']
        current_price = row['current_price']
        prev_close = row.get('prev_close', current_price)
        today_volume = row['today_volume']

        reasons: list[str] = []

        # Calculate metrics
        price_change = calculate_price_change(symbol, current_price, prev_close)

        # Get average volume (use cached value from stock_universe)
        avg_volume = get_average_volume(symbol)
        volume_ratio = today_volume / avg_volume if avg_volume > 0 else 1.0

        # Apply filters
        flag_stock = False

        # Rule 1: Price movement
        if abs(price_change) > PRICE_CHANGE_THRESHOLD:
            flag_stock = True
            reasons.append(f"price_{price_change:+.2f}%")

        # Rule 2: Volume spike
        if volume_ratio > VOLUME_RATIO_THRESHOLD:
            flag_stock = True
            reasons.append(f"volume_{volume_ratio:.1f}x")

        # Rule 3: Always flag F&O stocks
        if symbol in fo_stocks:
            flag_stock = True
            reasons.append("fo_stock")

        # Rule 4: Always flag stocks in open trades
        if symbol in open_trade_symbols:
            flag_stock = True
            reasons.append("open_trade")

        if flag_stock:
            flagged_stocks.append({
                'symbol': symbol,
                'price_change': price_change,
                'volume_ratio': volume_ratio,
                'reason': ', '.join(reasons),
                'current_price': current_price,
            })

    processing_duration = time.time() - processing_start

    # Write results to database
    write_start = time.time()
    inserted = write_stage1_results(flagged_stocks, scan_time)
    write_duration = time.time() - write_start

    total_duration = time.time() - scan_started

    print("-" * 64)
    print(f"  STAGE 1 COMPLETE")
    print(f"  Scanned: {total_stocks:,} stocks in {total_duration:.1f}s")
    print(f"  Flagged: {len(flagged_stocks)} stocks for Stage 2")
    print(f"  DB writes: {inserted} records")
    print(f"  Breakdown: fetch={price_fetch_duration:.1f}s, "
          f"process={processing_duration:.1f}s, write={write_duration:.1f}s")
    print("=" * 64 + "\n")

    return [stock['symbol'] for stock in flagged_stocks], scan_time


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    flagged, scan_time = run_stage1_scan()

    if flagged:
        print(f"  Flagged symbols ({len(flagged)}) at {scan_time}:")
        print(f"  {', '.join(flagged[:20])}")
        if len(flagged) > 20:
            print(f"  ... and {len(flagged) - 20} more")
    else:
        print("  No stocks flagged in this scan.")
