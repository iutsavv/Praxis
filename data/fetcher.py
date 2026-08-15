"""
fetcher.py â€” Raw OHLCV candle data fetcher for the AI Paper Trading Agent.

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

INTERVALS = {
    "5m": {"period": "5d", "priority": "HIGH"},
    "15m": {"period": "60d", "priority": "HIGH"},
    "1h": {"period": "60d", "priority": "MEDIUM"},
    "1d": {"period": "365d", "priority": "LOW"},
}
INTERVAL = "15m"
BACKFILL_DAYS = 59
INTERVAL_DELTAS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}
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
    """Read symbols from stock_universe for Stage 2 scanning, plus original stocks table."""

    # Get Stage 2 scan candidates from universe
    cursor = conn.execute(
        """SELECT symbol FROM stock_universe
           WHERE in_stage2_scan = 1 AND is_active = 1
           ORDER BY symbol"""
    )
    universe_symbols = [row[0] for row in cursor.fetchall()]

    # Get original stocks (for backward compatibility with trading)
    cursor = conn.execute(
        "SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol"
    )
    trading_symbols = [row[0] for row in cursor.fetchall()]

    # Combine both lists, removing duplicates
    all_symbols = list(set(universe_symbols + trading_symbols))

    # Market-condition detection depends on Nifty history
    if NIFTY_SYMBOL not in all_symbols:
        all_symbols.append(NIFTY_SYMBOL)

    return sorted(all_symbols)


def get_latest_timestamp(conn: sqlite3.Connection, symbol: str) -> str | None:
    """Return the latest stored timestamp for a symbol + interval, or None."""
    return get_latest_timestamp_for_interval(conn, symbol, INTERVAL)


def get_latest_timestamp_for_interval(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
) -> str | None:
    """Return the latest stored timestamp for a symbol and interval."""
    cursor = conn.execute(
        """
        SELECT MAX(timestamp) FROM candles
        WHERE symbol = ? AND interval = ?
        """,
        (symbol, interval),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Fetching logic
# ---------------------------------------------------------------------------

def fetch_candles(
    symbol: str,
    latest_ts: str | None = None,
    interval: str | None = None,
) -> pd.DataFrame:
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
    active_interval = interval or INTERVAL
    if active_interval not in INTERVALS:
        raise ValueError(f"Unsupported candle interval: {active_interval}")

    ticker = "^NSEI" if symbol in {"NSEI", "^NSEI"} else f"{symbol}.NS"
    interval_delta = INTERVAL_DELTAS.get(active_interval, timedelta(minutes=15))

    if latest_ts is None:
        df = yf.download(
            ticker,
            period=str(INTERVALS[active_interval]["period"]),
            interval=active_interval,
            progress=False,
            auto_adjust=True,
            # multi_level_cols=False for yfinance>=0.2.37
        )
    else:
        start_dt = pd.Timestamp(latest_ts) + interval_delta
        now = pd.Timestamp.now(tz=start_dt.tz)
        # If the next expected candle hasn't happened yet, skip the download
        if start_dt > now:
            return pd.DataFrame()
        df = yf.download(
            ticker,
            start=start_dt,
            interval=active_interval,
            progress=False,
            auto_adjust=True,
        )

    if df is None or df.empty:
        return pd.DataFrame()

    # yfinance sometimes returns MultiIndex columns â€” flatten them
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Keep only OHLCV columns
    expected = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        print(f"    [WARN] {symbol}: missing columns {missing} â€” skipping")
        return pd.DataFrame()

    df = df[expected].copy()

    # Drop any rows where all OHLCV values are NaN
    df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

    return df


def fetch_candles_bulk(symbols: list[str], interval: str = "15m") -> dict[str, pd.DataFrame]:
    """Fetch candles for multiple symbols at once using yfinance bulk download."""
    if not symbols:
        return {}

    active_interval = interval or INTERVAL
    tickers = [f"{sym}.NS" for sym in symbols]

    df_bulk = yf.download(
        tickers,
        period=str(INTERVALS[active_interval]["period"]),
        interval=active_interval,
        group_by="ticker",
        progress=False,
        threads=True,
        auto_adjust=True,
    )

    if df_bulk is None or df_bulk.empty:
        return {}

    result_dfs = {}

    # If only one symbol was provided, yfinance might not use MultiIndex depending on the version
    if not isinstance(df_bulk.columns, pd.MultiIndex):
        df_bulk.columns = pd.MultiIndex.from_product([[tickers[0]], df_bulk.columns])

    expected = ["Open", "High", "Low", "Close", "Volume"]

    for ticker in df_bulk.columns.levels[0]:
        symbol = ticker.replace(".NS", "")
        if symbol not in symbols:
            continue

        try:
            df = df_bulk[ticker].copy()
            # Drop rows where all OHLCV are NaN
            df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

            missing = [c for c in expected if c not in df.columns]
            if not missing and not df.empty:
                result_dfs[symbol] = df[expected]
        except KeyError:
            continue

    return result_dfs


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_candles(
    conn: sqlite3.Connection,
    symbol: str,
    df: pd.DataFrame,
    interval: str | None = None,
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
    active_interval = interval or INTERVAL

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
                    active_interval,
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
    """Fetch and store candles for every active symbol."""

    print("\n" + "=" * 64)
    print("  AI Paper Trading Agent â€” OHLCV Candle Fetcher")
    print("=" * 64)
    print(f"  Database : {DB_PATH}")
    print(f"  Interval : {INTERVAL}")
    print(f"  Period   : {INTERVALS[INTERVAL]['period']} ({INTERVALS[INTERVAL]['priority']} priority)")
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

    print(f"  Bulk downloading {len(symbols)} symbols...")
    bulk_data = fetch_candles_bulk(symbols, INTERVAL)
    mode = f"period ({INTERVALS[INTERVAL]['period']})"

    for i, symbol in enumerate(symbols, start=1):
        prefix = f"  [{i:3d}/{len(symbols)}] {symbol:<12}"
        try:
            df = bulk_data.get(symbol, pd.DataFrame())

            if df.empty:
                print(f"{prefix} | no new data")
                results.append((symbol, 0, 0, mode))
                continue

            inserted, skipped = store_candles(conn, symbol, df, INTERVAL)
            print(f"{prefix} | +{inserted} new, ~{skipped} skipped")
            results.append((symbol, inserted, skipped, mode))

        except Exception as e:
            print(f"{prefix} | [ERROR] {e}")
            results.append((symbol, 0, 0, f"error: {e}"))

    conn.close()

    # â”€â”€ Final summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


def run_fetcher_for_intervals(intervals: list[str] | tuple[str, ...] | None = None) -> None:
    """Fetch candles for the requested intervals.

    The module stores the active interval in one global because the existing
    helper functions share it. This wrapper keeps orchestrator calls explicit
    while restoring the original interval after each run.
    """
    global INTERVAL

    requested_intervals = list(intervals or [INTERVAL])
    original_interval = INTERVAL
    try:
        for interval in requested_intervals:
            if interval not in INTERVALS:
                raise ValueError(f"Unsupported candle interval: {interval}")
            INTERVAL = interval
            run_fetcher()
    finally:
        INTERVAL = original_interval


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_fetcher()
