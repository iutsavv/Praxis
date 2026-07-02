"""
fetcher.py — Raw OHLCV candle data fetcher for the AI Paper Trading Agent.

Single responsibility: fetch 15-minute candle data from Yahoo Finance (via
yfinance) for every active NSE symbol in the watchlist and store the raw
OHLCV rows in the `candles` table.  No indicator calculations happen here.

Incremental fetching
--------------------
On each run the script checks the latest stored timestamp per symbol.  If
data already exists it fetches only newer candles; otherwise it backfills
the last 60 days.

Usage:
    python data/fetcher.py
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "database", "trading.db")

INTERVAL = "15m"                     # only 15-minute candles
BACKFILL_DAYS = 59                   # initial load window (yfinance caps 15m at 60d)
NIFTY_SYMBOL = "NSEI"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    """Return a connection to trading.db, or exit if the DB doesn't exist."""
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        print("        Run  python database/init_db.py  first.")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def get_active_symbols(conn: sqlite3.Connection) -> list[str]:
    """Read all symbols from the stocks table where is_active = 1."""
    cursor = conn.execute(
        "SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol"
    )
    symbols = [row[0] for row in cursor.fetchall()]
    # Market-condition detection depends on Nifty history even though the
    # index is not a tradable watchlist stock.
    if NIFTY_SYMBOL not in symbols:
        symbols.append(NIFTY_SYMBOL)
    return symbols


def get_latest_timestamp(conn: sqlite3.Connection, symbol: str) -> str | None:
    """Return the latest stored timestamp for a symbol + interval, or None."""
    cursor = conn.execute(
        """
        SELECT MAX(timestamp) FROM candles
        WHERE symbol = ? AND interval = ?
        """,
        (symbol, INTERVAL),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Fetching logic
# ---------------------------------------------------------------------------

def fetch_candles(symbol: str, latest_ts: str | None) -> pd.DataFrame:
    """Download 15m OHLCV candles for an NSE symbol via yfinance.

    Parameters
    ----------
    symbol : str
        Bare NSE symbol (e.g. "RELIANCE").
    latest_ts : str or None
        ISO-8601 timestamp of the newest candle already stored.
        If None the last ``BACKFILL_DAYS`` days are fetched.

    Returns
    -------
    pd.DataFrame
        Columns: Open, High, Low, Close, Volume with a DatetimeIndex.
        Empty DataFrame if nothing new is available.
    """
    ticker = "^NSEI" if symbol in {"NSEI", "^NSEI"} else f"{symbol}.NS"

    if latest_ts is None:
        # Initial backfill — yfinance caps 15m data to ~60 days
        start_date = (datetime.now() - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
        df = yf.download(
            ticker,
            start=start_date,
            interval=INTERVAL,
            progress=False,
            auto_adjust=True,
            # multi_level_cols=False for yfinance>=0.2.37
        )
    else:
        # Incremental — fetch from one interval after the last stored candle
        start_dt = pd.Timestamp(latest_ts) + timedelta(minutes=15)
        df = yf.download(
            ticker,
            start=start_dt,
            interval=INTERVAL,
            progress=False,
            auto_adjust=True,
        )

    if df is None or df.empty:
        return pd.DataFrame()

    # yfinance sometimes returns MultiIndex columns — flatten them
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Keep only OHLCV columns
    expected = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        print(f"    [WARN] {symbol}: missing columns {missing} — skipping")
        return pd.DataFrame()

    df = df[expected].copy()

    # Drop any rows where all OHLCV values are NaN
    df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

    return df


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_candles(
    conn: sqlite3.Connection,
    symbol: str,
    df: pd.DataFrame,
) -> tuple[int, int]:
    """Write a DataFrame of candles into the candles table.

    Uses INSERT OR IGNORE to silently skip duplicates (matched by the
    UNIQUE constraint on symbol + interval + timestamp).

    Returns
    -------
    (inserted, skipped) : tuple[int, int]
    """
    if df.empty:
        return 0, 0

    cursor = conn.cursor()
    inserted = 0
    skipped = 0

    for idx, row in df.iterrows():
        ts = idx.strftime("%Y-%m-%d %H:%M:%S")
        try:
            cursor.execute(
                """
                INSERT OR IGNORE INTO candles
                    (symbol, interval, open, high, low, close, volume, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    INTERVAL,
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    int(row["Volume"]),
                    ts,
                ),
            )
            if cursor.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.Error as e:
            print(f"    [DB ERROR] {symbol} @ {ts}: {e}")
            skipped += 1

    conn.commit()
    return inserted, skipped


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_fetcher() -> None:
    """Fetch and store 15m candles for every active symbol."""

    print("\n" + "=" * 64)
    print("  AI Paper Trading Agent — OHLCV Candle Fetcher")
    print("=" * 64)
    print(f"  Database : {DB_PATH}")
    print(f"  Interval : {INTERVAL}")
    print(f"  Backfill : {BACKFILL_DAYS} days (for new symbols)")
    print("=" * 64 + "\n")

    conn = _get_connection()
    symbols = get_active_symbols(conn)

    if not symbols:
        print("[WARN] No active symbols found in the stocks table.")
        conn.close()
        return

    print(f"  Active symbols: {len(symbols)}")
    print(f"  Symbols: {', '.join(symbols)}\n")

    # Track per-symbol results for the final summary
    results: list[tuple[str, int, int, str]] = []

    for i, symbol in enumerate(symbols, start=1):
        prefix = f"  [{i:2d}/{len(symbols)}] {symbol:<12}"
        try:
            latest_ts = get_latest_timestamp(conn, symbol)
            mode = "incremental" if latest_ts else f"backfill ({BACKFILL_DAYS}d)"
            print(f"{prefix} | mode: {mode:<20}", end="", flush=True)

            df = fetch_candles(symbol, latest_ts)

            if df.empty:
                print(f" | no new data")
                results.append((symbol, 0, 0, mode))
                continue

            inserted, skipped = store_candles(conn, symbol, df)
            print(f" | +{inserted} new, ~{skipped} skipped")
            results.append((symbol, inserted, skipped, mode))

        except Exception as e:
            print(f" | [ERROR] {e}")
            results.append((symbol, 0, 0, f"error: {e}"))

    conn.close()

    # ── Final summary ────────────────────────────────────────────
    print("\n" + "-" * 64)
    print("  FETCH SUMMARY")
    print("-" * 64)
    print(f"  {'Symbol':<14} {'Inserted':>10} {'Skipped':>10}   Mode")
    print(f"  {'------':<14} {'--------':>10} {'-------':>10}   ----")

    total_inserted = 0
    total_skipped = 0
    for symbol, ins, skp, mode in results:
        print(f"  {symbol:<14} {ins:>10} {skp:>10}   {mode}")
        total_inserted += ins
        total_skipped += skp

    print(f"  {'-'*14} {'-'*10} {'-'*10}")
    print(f"  {'TOTAL':<14} {total_inserted:>10} {total_skipped:>10}")
    print("-" * 64)
    print(f"  Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 64 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_fetcher()
