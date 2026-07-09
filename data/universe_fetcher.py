"""
universe_fetcher.py - NSE Stock Universe Manager

Single responsibility: maintain the stock_universe table with all NSE listed stocks.
Downloads complete NSE equity list and F&O lists, classifies stocks, and manages universe.

Usage:
    python data/universe_fetcher.py
"""

import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "database" / "trading.db"

# NSE URLs
NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_FO_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"

# Headers for NSE requests
NSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Referer': 'https://www.nseindia.com',
    'Accept': 'text/csv,application/csv,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive',
}

# Market cap thresholds (in crores)
LARGE_CAP_THRESHOLD = 20000  # ₹20,000 crores
MID_CAP_THRESHOLD = 5000     # ₹5,000 crores
STAGE2_VOLUME_THRESHOLD = 500000  # 5,00,000 shares daily average


def get_session() -> requests.Session:
    """Create session with retry strategy."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        backoff_factor=1
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(NSE_HEADERS)
    return session


def fetch_nse_equity_list() -> pd.DataFrame:
    """Download complete NSE equity list.
    
    Returns
    -------
    pd.DataFrame
        DataFrame with all NSE listed stocks
        
    Raises
    ------
    Exception
        If download fails after 3 retries
    """
    print("  [INFO] Fetching NSE equity list...")
    
    session = get_session()
    
    for attempt in range(3):
        try:
            response = session.get(NSE_EQUITY_LIST_URL, timeout=30)
            response.raise_for_status()
            
            # Parse CSV
            from io import StringIO
            df = pd.read_csv(StringIO(response.text))
            
            # Clean and validate
            if 'SYMBOL' not in df.columns:
                raise ValueError("Invalid CSV format - missing SYMBOL column")
            
            # Standardize column names
            df = df.rename(columns={
                'SYMBOL': 'symbol',
                'NAME OF COMPANY': 'name',
                ' SERIES': 'series',
                'DATE OF LISTING': 'listing_date'
            })
            
            # Clean data
            df['symbol'] = df['symbol'].str.strip().str.upper()
            df['series'] = df['series'].str.strip() if 'series' in df.columns else 'EQ'
            
            # Filter to EQ series only (exclude bonds, warrants, etc.)
            df = df[df['series'].isin(['EQ', 'BE'])]
            
            print(f"  [OK] Downloaded {len(df)} equity stocks from NSE")
            return df
            
        except Exception as e:
            print(f"  [ERROR] Attempt {attempt + 1}/3 failed: {e}")
            if attempt == 2:
                raise Exception(f"Failed to fetch NSE equity list after 3 attempts: {e}")
            time.sleep(2)


def fetch_fo_stock_list() -> list:
    """Download F&O eligible stocks list.
    
    Returns
    -------
    list
        List of symbols that are F&O eligible
        
    Raises
    ------
    Exception
        If download fails after 3 retries
    """
    print("  [INFO] Fetching F&O stocks list...")
    
    session = get_session()
    
    for attempt in range(3):
        try:
            response = session.get(NSE_FO_LIST_URL, timeout=30)
            response.raise_for_status()
            
            # Parse CSV
            from io import StringIO
            df = pd.read_csv(StringIO(response.text))
            
            print(f"    [DEBUG] F&O CSV columns: {list(df.columns)}")
            
            # Extract symbols - look for SYMBOL column (with possible whitespace)
            symbol_col = None
            for col in df.columns:
                if 'SYMBOL' in col.strip().upper():
                    symbol_col = col
                    break
            
            if symbol_col:
                symbols = df[symbol_col].str.strip().str.upper().dropna().unique().tolist()
                
                # Filter out obvious non-symbols
                filtered_symbols = []
                for symbol in symbols:
                    if isinstance(symbol, str) and len(symbol) >= 2 and symbol.replace('-', '').replace('&', '').isalnum():
                        filtered_symbols.append(symbol)
                
                print(f"  [OK] Downloaded {len(filtered_symbols)} F&O eligible stocks")
                return filtered_symbols
            else:
                raise ValueError("F&O CSV has no SYMBOL column")
            
        except Exception as e:
            print(f"  [ERROR] Attempt {attempt + 1}/3 failed: {e}")
            if attempt == 2:
                # If F&O fetch fails, use a fallback list of known F&O stocks
                print(f"  [WARN] Using fallback F&O list")
                fallback_fo_stocks = [
                    'RELIANCE', 'INFY', 'HDFCBANK', 'ICICIBANK', 'TCS', 'AXISBANK', 
                    'SBIN', 'BAJFINANCE', 'MARUTI', 'WIPRO', 'HINDUNILVR', 'SUNPHARMA',
                    'ADANIPORTS', 'INDIGO', 'TATAMOTORS', 'LT', 'KOTAKBANK', 'BHARTIARTL',
                    'ITC', 'ASIANPAINT', 'TECHM', 'HCLTECH', 'POWERGRID', 'NTPC',
                    'ONGC', 'IOC', 'GRASIM', 'COALINDIA', 'DRREDDY', 'CIPLA',
                    'BAJAJFINSV', 'NESTLEIND', 'SHREECEM', 'ULTRACEMCO', 'JSWSTEEL'
                ]
                return fallback_fo_stocks
            time.sleep(2)


def fetch_stock_metadata(symbol: str) -> dict[str, Any]:
    """Get stock metadata from yfinance.
    
    Parameters
    ----------
    symbol : str
        Stock symbol (e.g., 'RELIANCE')
        
    Returns
    -------
    dict
        Metadata including market cap, volume, sector, industry
    """
    try:
        # Add .NS suffix for NSE stocks
        ticker = yf.Ticker(f"{symbol}.NS")
        info = ticker.info
        
        # Get historical data for volume calculation
        try:
            hist = ticker.history(period="30d")
            avg_volume = hist['Volume'].mean() if not hist.empty else 0
        except:
            avg_volume = 0
        
        return {
            'market_cap': info.get('marketCap', 0) / 10000000,  # Convert to crores
            'avg_daily_volume': avg_volume,
            'sector': info.get('sector', ''),
            'industry': info.get('industry', ''),
        }
        
    except Exception as e:
        print(f"  [WARN] Failed to get metadata for {symbol}: {e}")
        return {
            'market_cap': 0,
            'avg_daily_volume': 0,
            'sector': '',
            'industry': '',
        }


def classify_stocks(df: pd.DataFrame) -> pd.DataFrame:
    """Add classification columns to stocks DataFrame.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with stock data including market_cap and avg_daily_volume
        
    Returns
    -------
    pd.DataFrame
        DataFrame with classification columns added
    """
    print("  [INFO] Classifying stocks...")
    
    # Market cap classification
    df['is_large_cap'] = (df['market_cap'] >= LARGE_CAP_THRESHOLD).astype(int)
    df['is_mid_cap'] = ((df['market_cap'] >= MID_CAP_THRESHOLD) & 
                        (df['market_cap'] < LARGE_CAP_THRESHOLD)).astype(int)
    df['is_small_cap'] = (df['market_cap'] < MID_CAP_THRESHOLD).astype(int)
    
    # Stage 2 scan eligibility (high volume OR F&O eligible)
    df['in_stage2_scan'] = (
        (df['avg_daily_volume'] >= STAGE2_VOLUME_THRESHOLD) | 
        (df['is_fo_stock'] == 1)
    ).astype(int)
    
    return df


def update_universe() -> dict[str, Any]:
    """Main function to update the stock universe.
    
    Returns
    -------
    dict
        Summary of update operation
    """
    print("\n" + "=" * 60)
    print("  NSE Stock Universe Update")
    print("=" * 60)
    
    start_time = time.time()
    
    try:
        # Download data
        equity_df = fetch_nse_equity_list()
        fo_symbols = fetch_fo_stock_list()
        
        # Connect to database
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Create table if needed
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_universe (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol              TEXT UNIQUE NOT NULL,
                name                TEXT,
                sector              TEXT,
                industry            TEXT,
                series              TEXT,
                market_cap          REAL,
                avg_daily_volume    REAL,
                is_fo_stock         INTEGER DEFAULT 0,
                is_large_cap        INTEGER DEFAULT 0,
                is_mid_cap          INTEGER DEFAULT 0,
                is_small_cap        INTEGER DEFAULT 0,
                is_active           INTEGER DEFAULT 1,
                in_stage2_scan      INTEGER DEFAULT 0,
                listing_date        TEXT,
                last_updated        TEXT,
                added_at            TEXT DEFAULT (datetime('now'))
            )
        """)
        
        # Get existing symbols
        existing_symbols = set(row[0] for row in cursor.execute(
            "SELECT symbol FROM stock_universe WHERE is_active = 1"
        ).fetchall())
        
        new_stocks = 0
        updated_stocks = 0
        fo_count = 0
        
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"  [INFO] Processing {len(equity_df)} stocks...")
        
        # Process in batches to avoid timeout
        batch_size = 50
        for batch_start in range(0, len(equity_df), batch_size):
            batch_end = min(batch_start + batch_size, len(equity_df))
            batch_df = equity_df.iloc[batch_start:batch_end]
            
            print(f"    Processing batch {batch_start//batch_size + 1}/{(len(equity_df)-1)//batch_size + 1} ({batch_start+1}-{batch_end})...")
            
            for idx, row in batch_df.iterrows():
                symbol = row['symbol']
                
                # Check if F&O eligible
                is_fo = 1 if symbol in fo_symbols else 0
                if is_fo:
                    fo_count += 1
                
                # Only get metadata for F&O stocks and top 200 by market importance
                # This speeds up the initial load significantly
                if is_fo or symbol in ['RELIANCE', 'INFY', 'HDFCBANK', 'ICICIBANK', 'TCS', 
                                      'AXISBANK', 'SBIN', 'BAJFINANCE', 'MARUTI', 'WIPRO',
                                      'HINDUNILVR', 'SUNPHARMA', 'ADANIPORTS', 'INDIGO', 'LT']:
                    metadata = fetch_stock_metadata(symbol)
                    market_cap = metadata['market_cap']
                    avg_volume = metadata['avg_daily_volume'] 
                    sector = metadata['sector']
                    industry = metadata['industry']
                else:
                    # Use default values for other stocks - can be updated later
                    market_cap = 1000  # Assume small cap
                    avg_volume = 50000  # Below Stage 2 threshold
                    sector = ''
                    industry = ''
                
                # Calculate classifications
                is_large_cap = 1 if market_cap >= LARGE_CAP_THRESHOLD else 0
                is_mid_cap = 1 if MID_CAP_THRESHOLD <= market_cap < LARGE_CAP_THRESHOLD else 0
                is_small_cap = 1 if market_cap < MID_CAP_THRESHOLD else 0
                in_stage2_scan = 1 if (avg_volume >= STAGE2_VOLUME_THRESHOLD or is_fo) else 0
                
                # Insert or update
                cursor.execute("""
                    INSERT OR REPLACE INTO stock_universe (
                        symbol, name, series, listing_date, sector, industry,
                        market_cap, avg_daily_volume, is_fo_stock,
                        is_large_cap, is_mid_cap, is_small_cap,
                        in_stage2_scan, is_active, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    symbol,
                    row.get('name', ''),
                    row.get('series', 'EQ'),
                    row.get('listing_date', ''),
                    sector,
                    industry,
                    market_cap,
                    avg_volume,
                    is_fo,
                    is_large_cap,
                    is_mid_cap,
                    is_small_cap,
                    in_stage2_scan,
                    current_time
                ))
                
                if symbol not in existing_symbols:
                    new_stocks += 1
                else:
                    updated_stocks += 1
        
        # Deactivate stocks no longer in NSE list
        current_symbols = set(equity_df['symbol'].tolist())
        deactivated = cursor.execute("""
            UPDATE stock_universe 
            SET is_active = 0, last_updated = ?
            WHERE symbol NOT IN ({}) AND is_active = 1
        """.format(','.join('?' * len(current_symbols))),
        [current_time] + list(current_symbols)
        ).rowcount
        
        conn.commit()
        
        # Get final counts
        total_stocks = cursor.execute("SELECT COUNT(*) FROM stock_universe WHERE is_active = 1").fetchone()[0]
        stage2_count = cursor.execute("SELECT COUNT(*) FROM stock_universe WHERE in_stage2_scan = 1 AND is_active = 1").fetchone()[0]
        
        conn.close()
        
        duration = time.time() - start_time
        
        summary = {
            'total_stocks': total_stocks,
            'new_stocks': new_stocks,
            'updated_stocks': updated_stocks,
            'deactivated_stocks': deactivated,
            'fo_stocks': fo_count,
            'stage2_eligible': stage2_count,
            'duration_seconds': round(duration, 2)
        }
        
        print(f"\n  [SUCCESS] Universe update completed in {duration:.1f}s")
        print(f"    Total active stocks: {total_stocks:,}")
        print(f"    New stocks added: {new_stocks:,}")
        print(f"    Stocks updated: {updated_stocks:,}")
        print(f"    Stocks deactivated: {deactivated}")
        print(f"    F&O eligible: {fo_count}")
        print(f"    Stage 2 scan eligible: {stage2_count:,}")
        
        return summary
        
    except Exception as e:
        print(f"\n  [ERROR] Universe update failed: {e}")
        import traceback
        traceback.print_exc()
        raise


def get_stage2_candidates() -> list[str]:
    """Get all symbols eligible for stage 2 scanning.
    
    Returns
    -------
    list
        List of symbols where in_stage2_scan = 1 and is_active = 1
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    symbols = [row[0] for row in cursor.execute(
        "SELECT symbol FROM stock_universe WHERE in_stage2_scan = 1 AND is_active = 1 ORDER BY symbol"
    ).fetchall()]
    
    conn.close()
    return symbols


def get_fo_stocks() -> list[str]:
    """Get F&O eligible symbols only.
    
    Returns
    -------
    list
        List of F&O eligible symbols
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    symbols = [row[0] for row in cursor.execute(
        "SELECT symbol FROM stock_universe WHERE is_fo_stock = 1 AND is_active = 1 ORDER BY symbol"
    ).fetchall()]
    
    conn.close()
    return symbols


def get_sector_distribution() -> dict[str, int]:
    """Get distribution of stocks by sector.
    
    Returns
    -------
    dict
        Sector name to count mapping
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    sectors = {}
    for row in cursor.execute("""
        SELECT sector, COUNT(*) as count 
        FROM stock_universe 
        WHERE is_active = 1 AND sector != '' 
        GROUP BY sector 
        ORDER BY count DESC
    """).fetchall():
        sectors[row[0]] = row[1]
    
    conn.close()
    return sectors


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  NSE Stock Universe Fetcher - Standalone Test")
    print("=" * 60)
    
    try:
        # Run universe update
        summary = update_universe()
        
        # Get additional stats
        stage2_candidates = get_stage2_candidates()
        fo_stocks = get_fo_stocks()
        sector_dist = get_sector_distribution()
        
        print("\n" + "-" * 60)
        print("  FINAL VERIFICATION")
        print("-" * 60)
        
        print(f"  Total stocks in universe: {summary['total_stocks']:,}")
        print(f"  F&O eligible stocks: {len(fo_stocks)}")
        print(f"  Stage 2 scan eligible: {len(stage2_candidates):,}")
        
        print(f"\n  Top 10 sectors by stock count:")
        for sector, count in list(sector_dist.items())[:10]:
            print(f"    {sector}: {count}")
        
        print(f"\n  Sample F&O stocks: {', '.join(fo_stocks[:10])}")
        print(f"  Sample Stage 2 candidates: {', '.join(stage2_candidates[:10])}")
        
        # Verify database
        if summary['total_stocks'] >= 2000:
            print(f"\n  ✅ SUCCESS: Database has {summary['total_stocks']:,} stocks (expected > 2000)")
        else:
            print(f"\n  ❌ WARNING: Database has only {summary['total_stocks']:,} stocks (expected > 2000)")
        
        if len(fo_stocks) >= 150:
            print(f"  ✅ SUCCESS: {len(fo_stocks)} F&O stocks found (expected ~180)")
        else:
            print(f"  ❌ WARNING: Only {len(fo_stocks)} F&O stocks found (expected ~180)")
        
        if len(stage2_candidates) >= 400:
            print(f"  ✅ SUCCESS: {len(stage2_candidates):,} Stage 2 candidates (expected 400-600)")
        else:
            print(f"  ❌ WARNING: Only {len(stage2_candidates):,} Stage 2 candidates (expected 400-600)")
        
    except Exception as e:
        print(f"\n  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("  Test complete")
    print("=" * 60 + "\n")