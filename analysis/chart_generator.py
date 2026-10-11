"""Professional mplfinance chart rendering for stored NSE paper-trading data."""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
CHART_DIR = PROJECT_ROOT / "charts"
INTRADAY_INTERVALS = {"1m", "3m", "5m", "10m", "15m", "30m", "45m", "60m", "1h"}

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _read_chart_frame(conn: sqlite3.Connection, symbol: str, interval: str, candles: int) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT c.timestamp, c.open, c.high, c.low, c.close, c.volume,
               i.ema_20, i.ema_50, i.vwap, i.rsi, i.macd, i.macd_signal,
               i.macd_histogram
        FROM candles c
        LEFT JOIN indicators i
          ON UPPER(i.symbol) = UPPER(c.symbol)
         AND i.interval = c.interval
         AND i.timestamp = c.timestamp
        WHERE UPPER(c.symbol) = UPPER(?) AND c.interval = ?
        ORDER BY c.timestamp DESC
        LIMIT ?
        """,
        (symbol, interval, max(20, int(candles))),
    ).fetchall()
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(_rows_to_dicts(rows)).sort_values("timestamp")
    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ema_20",
        "ema_50",
        "vwap",
        "rsi",
        "macd",
        "macd_signal",
        "macd_histogram",
    ]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.set_index("timestamp")
    frame.index.name = "Date"
    frame = frame.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    if frame["ema_20"].isna().all():
        frame["ema_20"] = frame["Close"].ewm(span=20, adjust=False, min_periods=1).mean()
    if frame["ema_50"].isna().all():
        frame["ema_50"] = frame["Close"].ewm(span=50, adjust=False, min_periods=1).mean()
    if frame["vwap"].isna().all():
        typical_price = (frame["High"] + frame["Low"] + frame["Close"]) / 3
        cumulative_volume = frame["Volume"].replace(0, pd.NA).fillna(0).cumsum()
        cumulative_value = (typical_price * frame["Volume"].fillna(0)).cumsum()
        frame["vwap"] = cumulative_value / cumulative_volume.replace(0, pd.NA)
    if frame["rsi"].isna().all():
        delta = frame["Close"].diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, pd.NA)
        frame["rsi"] = (100 - (100 / (1 + rs))).fillna(50)
    if frame["macd"].isna().all() or frame["macd_signal"].isna().all():
        ema_12 = frame["Close"].ewm(span=12, adjust=False, min_periods=1).mean()
        ema_26 = frame["Close"].ewm(span=26, adjust=False, min_periods=1).mean()
        frame["macd"] = ema_12 - ema_26
        frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
        frame["macd_histogram"] = frame["macd"] - frame["macd_signal"]
    frame["avg_volume_10"] = frame["Volume"].rolling(10, min_periods=1).mean()
    return frame


def _read_levels(conn: sqlite3.Connection, symbol: str, interval: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "price_levels"):
        return []
    rows = conn.execute(
        """
        SELECT level_price, level_type, touches, strength_score, last_touch
        FROM price_levels
        WHERE UPPER(symbol) = UPPER(?)
          AND timeframe = ?
          AND COALESCE(is_active, 1) = 1
        ORDER BY strength_score DESC, touches DESC
        LIMIT 12
        """,
        (symbol, interval),
    ).fetchall()
    return _rows_to_dicts(rows)


def _read_patterns(conn: sqlite3.Connection, symbol: str, interval: str, timestamps: list[str]) -> list[dict[str, Any]]:
    if not timestamps or not _table_exists(conn, "patterns"):
        return []
    placeholders = ",".join("?" for _ in timestamps)
    rows = conn.execute(
        f"""
        SELECT timestamp, pattern_name, direction, strength
        FROM patterns
        WHERE UPPER(symbol) = UPPER(?)
          AND interval = ?
          AND timestamp IN ({placeholders})
        ORDER BY timestamp ASC, strength DESC
        """,
        (symbol, interval, *timestamps),
    ).fetchall()
    return _rows_to_dicts(rows)


def _read_open_trade(conn: sqlite3.Connection, symbol: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "paper_trades"):
        return None
    rows = conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE UPPER(symbol) = UPPER(?) AND status = 'OPEN'
        ORDER BY entry_time DESC, id DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchall()
    return dict(rows[0]) if rows else None


def _read_latest_signal(conn: sqlite3.Connection, symbol: str) -> dict[str, Any] | None:
    if _table_exists(conn, "scan_results"):
        row = conn.execute(
            """
            SELECT symbol, stage2_score AS score, stage2_direction AS direction,
                   stage2_pattern AS pattern, stage2_setup AS setup,
                   scan_time AS created_at
            FROM scan_results
            WHERE UPPER(symbol) = UPPER(?) AND stage = 2
            ORDER BY scan_time DESC, id DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row:
            return dict(row)
    if not _table_exists(conn, "signal_scores"):
        return None
    row = conn.execute(
        """
        SELECT symbol, confidence_score AS score, direction, explanation AS setup,
               created_at
        FROM signal_scores
        WHERE UPPER(symbol) = UPPER(?)
        ORDER BY scan_cycle DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def _read_latest_candle_summary(conn: sqlite3.Connection, symbol: str, interval: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT close, volume, timestamp
        FROM candles
        WHERE UPPER(symbol) = UPPER(?) AND interval = ?
        ORDER BY timestamp DESC
        LIMIT 2
        """,
        (symbol, interval),
    ).fetchall()
    if not rows:
        return {"current_price": None, "change_pct": None, "volume": None, "timestamp": None}
    latest = rows[0]
    previous_close = float(rows[1]["close"]) if len(rows) > 1 and rows[1]["close"] else None
    current = float(latest["close"])
    change_pct = ((current - previous_close) / previous_close * 100) if previous_close else None
    return {
        "current_price": current,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "volume": latest["volume"],
        "timestamp": latest["timestamp"],
    }


def _read_news(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    if not _table_exists(conn, "news"):
        return {"score": 0.0, "headlines": []}
    rows = conn.execute(
        """
        SELECT headline, sentiment_score
        FROM news
        WHERE UPPER(symbol) = UPPER(?) OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?
        ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
        LIMIT 5
        """,
        (symbol, f"%{symbol.upper()}%"),
    ).fetchall()
    scores = [float(row["sentiment_score"] or 0) for row in rows]
    return {
        "score": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "headlines": [row["headline"] for row in rows[:2]],
    }


def _line_value(trade: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = trade.get(name)
        if value not in (None, ""):
            return float(value)
    return None


def _add_horizontal_line(ax: Any, price: float, label: str, color: str, linestyle: str, linewidth: float = 1.0) -> None:
    ax.axhline(price, color=color, linestyle=linestyle, linewidth=linewidth, alpha=0.92)
    ax.text(
        0.995,
        price,
        label,
        color=color,
        fontsize=7.5,
        ha="right",
        va="bottom",
        transform=ax.get_yaxis_transform(),
        bbox={"facecolor": "#0f1117", "edgecolor": color, "alpha": 0.62, "pad": 2},
    )


def generate_chart(symbol: str, interval: str = "15m", candles: int = 100) -> bytes:
    """Generate and persist a PNG candlestick chart, returning its bytes."""
    normalized_symbol = symbol.upper().strip()
    normalized_interval = interval.strip()

    with _connect() as conn:
        frame = _read_chart_frame(conn, normalized_symbol, normalized_interval, candles)
        if frame.empty:
            raise ValueError(f"No candle data found for {normalized_symbol} {normalized_interval}")

        timestamp_texts = [idx.strftime("%Y-%m-%d %H:%M:%S") for idx in frame.index]
        levels = _read_levels(conn, normalized_symbol, normalized_interval)
        patterns = _read_patterns(conn, normalized_symbol, normalized_interval, timestamp_texts)
        trade = _read_open_trade(conn, normalized_symbol)

    up_color = "#00c896"
    down_color = "#ff4d6d"
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        facecolor="#0f1117",
        figcolor="#0f1117",
        edgecolor="#2c3444",
        gridcolor="#293142",
        gridstyle="--",
        marketcolors=mpf.make_marketcolors(
            up=up_color,
            down=down_color,
            edge="inherit",
            wick="inherit",
            volume="inherit",
        ),
        rc={
            "axes.labelcolor": "#eef3f8",
            "axes.edgecolor": "#2c3444",
            "xtick.color": "#9aa6b7",
            "ytick.color": "#9aa6b7",
            "text.color": "#eef3f8",
            "font.size": 8,
        },
    )

    addplots: list[Any] = []
    if frame["ema_20"].notna().any():
        addplots.append(mpf.make_addplot(frame["ema_20"], panel=0, color="#3b82f6", width=1.1, label="EMA20"))
    if frame["ema_50"].notna().any():
        addplots.append(mpf.make_addplot(frame["ema_50"], panel=0, color="#f59e0b", width=1.1, label="EMA50"))
    if normalized_interval in INTRADAY_INTERVALS and frame["vwap"].notna().any():
        addplots.append(mpf.make_addplot(frame["vwap"], panel=0, color="#a855f7", width=1.0, label="VWAP"))

    volume_colors = [up_color if close > open_ else down_color for open_, close in zip(frame["Open"], frame["Close"])]
    addplots.append(mpf.make_addplot(frame["Volume"].fillna(0), panel=1, type="bar", color=volume_colors, alpha=0.55, ylabel="Volume"))
    addplots.append(mpf.make_addplot(frame["avg_volume_10"].fillna(0), panel=1, color="#9ca3af", linestyle="--", width=1.0, label="10 avg vol"))
    if frame["rsi"].notna().any():
        addplots.append(mpf.make_addplot(frame["rsi"], panel=2, color="#3b82f6", width=1.0, ylabel="RSI"))
        rsi_oversold = frame["rsi"].where(frame["rsi"] < 35)
        rsi_overbought = frame["rsi"].where(frame["rsi"] > 65)
        if rsi_oversold.notna().any():
            addplots.append(mpf.make_addplot(rsi_oversold, panel=2, color=up_color, width=1.5))
        if rsi_overbought.notna().any():
            addplots.append(mpf.make_addplot(rsi_overbought, panel=2, color=down_color, width=1.5))
    if frame["macd"].notna().any():
        addplots.append(mpf.make_addplot(frame["macd"], panel=3, color="#3b82f6", width=1.0, ylabel="MACD"))
    if frame["macd_signal"].notna().any():
        addplots.append(mpf.make_addplot(frame["macd_signal"], panel=3, color="#f59e0b", width=1.0))
    if frame["macd_histogram"].notna().any():
        addplots.append(
            mpf.make_addplot(
                frame["macd_histogram"],
                panel=3,
                type="bar",
                color=[up_color if value >= 0 else down_color for value in frame["macd_histogram"].fillna(0)],
                alpha=0.55,
            )
        )

    fig, axes = mpf.plot(
        frame,
        type="candle",
        style=style,
        addplot=addplots,
        panel_ratios=(5, 1.35, 1.25, 1.45),
        figsize=(14, 9),
        tight_layout=True,
        returnfig=True,
        xrotation=15,
        datetime_format="%d %b %H:%M" if normalized_interval != "1d" else "%d %b",
    )

    main_ax = axes[0]
    rsi_ax = axes[4]
    macd_ax = axes[6]

    main_ax.set_title(f"{normalized_symbol} / {normalized_interval}", color="#eef3f8", fontsize=13, pad=12)
    for level in levels:
        price = float(level["level_price"])
        touches = int(level.get("touches") or 0)
        level_type = str(level["level_type"]).upper()
        if level_type == "SUPPORT":
            _add_horizontal_line(main_ax, price, f"S: ₹{price:.2f} ({touches} touches)", up_color, "--")
        elif level_type == "RESISTANCE":
            _add_horizontal_line(main_ax, price, f"R: ₹{price:.2f} ({touches} touches)", down_color, "--")

    if trade:
        entry = _line_value(trade, "entry_price")
        target = _line_value(trade, "target_price")
        stop = _line_value(trade, "stop_loss_price")
        trailing = _line_value(trade, "trailing_stop_price", "trail_stop_price", "trailing_stop")
        if entry is not None:
            _add_horizontal_line(main_ax, entry, f"Entry ₹{entry:.2f}", "#3b82f6", "-", 1.3)
        if target is not None:
            _add_horizontal_line(main_ax, target, f"Target ₹{target:.2f}", up_color, "-", 1.3)
        if stop is not None:
            _add_horizontal_line(main_ax, stop, f"SL ₹{stop:.2f}", down_color, "-", 1.3)
        if trailing is not None:
            _add_horizontal_line(main_ax, trailing, f"Trail ₹{trailing:.2f}", "#f59e0b", "--", 1.2)

    for pattern in patterns[-6:]:
        ts = pd.to_datetime(pattern["timestamp"], errors="coerce")
        if pd.isna(ts) or ts not in frame.index:
            continue
        y_value = float(frame.loc[ts, "High"])
        main_ax.annotate(
            str(pattern["pattern_name"]),
            xy=(ts, y_value),
            xytext=(0, 18),
            textcoords="offset points",
            color="#f8fafc",
            fontsize=7.5,
            ha="center",
            arrowprops={"arrowstyle": "->", "color": "#cbd5e1", "lw": 0.8},
            bbox={"facecolor": "#1a1d27", "edgecolor": "#64748b", "alpha": 0.86, "pad": 2},
        )

    rsi_ax.axhline(70, color=down_color, linestyle="--", linewidth=0.9, alpha=0.8)
    rsi_ax.axhline(30, color=up_color, linestyle="--", linewidth=0.9, alpha=0.8)
    rsi_ax.set_ylim(0, 100)
    macd_ax.axhline(0, color="#64748b", linewidth=0.8, alpha=0.75)

    for ax in fig.axes:
        ax.tick_params(colors="#9aa6b7")
        ax.yaxis.label.set_color("#eef3f8")
        ax.xaxis.label.set_color("#eef3f8")
        handles, labels = ax.get_legend_handles_labels()
        if handles and labels:
            legend = ax.legend(loc="upper left", fontsize=7, frameon=True)
            legend.get_frame().set_facecolor("#0f1117")
            legend.get_frame().set_edgecolor("#2c3444")
            for text in legend.get_texts():
                text.set_color("#eef3f8")

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=130, facecolor="#0f1117", bbox_inches="tight")
    plt.close(fig)

    chart_bytes = buffer.getvalue()
    CHART_DIR.mkdir(exist_ok=True)
    (CHART_DIR / f"{normalized_symbol}_{normalized_interval}.png").write_bytes(chart_bytes)
    return chart_bytes


def get_active_chart_symbols() -> list[dict[str, Any]]:
    """Return active symbols with latest score/direction/pattern for chart navigation."""
    with _connect() as conn:
        source_table = "stock_universe" if _table_exists(conn, "stock_universe") else "stocks"
        rows = conn.execute(
            f"""
            SELECT symbol, name, sector
            FROM {source_table}
            WHERE COALESCE(is_active, 1) = 1
            ORDER BY symbol
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            signal = _read_latest_signal(conn, item["symbol"]) or {}
            item.update(
                {
                    "score": signal.get("score"),
                    "direction": signal.get("direction") or "HOLD",
                    "pattern": signal.get("pattern") or signal.get("setup"),
                }
            )
            result.append(item)
        return result


def get_chart_context(symbol: str, interval: str = "15m") -> dict[str, Any]:
    """Return the non-image data used by the frontend chart workspace."""
    normalized_symbol = symbol.upper().strip()
    with _connect() as conn:
        latest = _read_latest_candle_summary(conn, normalized_symbol, interval)
        latest_ts = latest.get("timestamp")
        pattern_rows = []
        if latest_ts:
            pattern_rows = _read_patterns(conn, normalized_symbol, interval, [str(latest_ts)])
        levels = _read_levels(conn, normalized_symbol, interval)
        trade = _read_open_trade(conn, normalized_symbol)
        signal = _read_latest_signal(conn, normalized_symbol)
        news = _read_news(conn, normalized_symbol)

    current_price = latest.get("current_price")
    if trade and current_price is not None:
        quantity = int(trade.get("quantity") or 0)
        entry = float(trade.get("entry_price") or current_price)
        multiplier = 1 if str(trade.get("direction")).upper() == "BUY" else -1
        trade["current_price"] = current_price
        trade["unrealized_pnl"] = round((current_price - entry) * quantity * multiplier, 2)

    trade_plan = None
    if not trade and signal and current_price is not None and signal.get("direction") in {"BUY", "SELL"}:
        try:
            from agents import risk_manager

            trade_plan = risk_manager.calculate_position(normalized_symbol, str(signal["direction"]), float(current_price))
            trade_plan["setup_name"] = signal.get("setup") or signal.get("pattern")
            trade_plan["confidence"] = signal.get("score")
        except Exception as exc:
            trade_plan = {"error": str(exc)}

    return {
        "symbol": normalized_symbol,
        "interval": interval,
        "summary": latest,
        "patterns": pattern_rows,
        "levels": levels,
        "news": news,
        "trade": trade,
        "signal": signal,
        "trade_plan": trade_plan,
    }


def generate_all_charts() -> dict[str, Any]:
    """Generate 15m charts for all active stocks. Intended after each scan cycle."""
    generated = 0
    failed: list[dict[str, str]] = []
    for stock in get_active_chart_symbols():
        symbol = str(stock["symbol"])
        try:
            generate_chart(symbol, "15m")
            generated += 1
        except Exception as exc:
            failed.append({"symbol": symbol, "error": str(exc)})
    return {"count_generated": generated, "failed": failed}
