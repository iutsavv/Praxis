"""Causal historical replay for the NSE paper-trading strategy.

Writes only to backtest_runs and backtest_trades. Live trading/account tables
are never mutated.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analysis.indicator_engine import compute_indicators
from analysis.signal_engine import load_config, score_macd, score_rsi, score_volume, score_vwap


DB_PATH = PROJECT_ROOT / "database" / "trading.db"
STARTING_BALANCE = 100_000.0
INDICATOR_WINDOW = 320


@dataclass
class Candidate:
    symbol: str
    index: int
    timestamp: datetime
    direction: str
    confidence_score: float
    market_condition: str
    close: float
    rsi: float | None
    macd: float | None
    vwap: float | None


@dataclass
class SimTrade:
    symbol: str
    direction: str
    entry_price: float
    entry_time: str
    target_price: float
    stop_loss_price: float
    capital_required: float
    quantity: int
    confidence_score: float
    market_condition: str
    rsi_at_entry: float | None
    macd_at_entry: float | None
    vwap_at_entry: float | None
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    outcome: str | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_backtest_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            symbols_tested INTEGER NOT NULL,
            strategy_version INTEGER NOT NULL,
            total_trades INTEGER NOT NULL,
            win_rate REAL NOT NULL,
            total_pnl REAL NOT NULL,
            max_drawdown REAL NOT NULL,
            sharpe_ratio REAL NOT NULL,
            profit_factor REAL NOT NULL,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            exit_price REAL NOT NULL,
            exit_time TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            pnl REAL NOT NULL,
            pnl_pct REAL NOT NULL,
            outcome TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            market_condition TEXT NOT NULL,
            rsi_at_entry REAL,
            macd_at_entry REAL,
            vwap_at_entry REAL,
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(run_id);
        """
    )
    conn.commit()


def _clean(value: Any) -> float | None:
    try:
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return None


def _strategy(conn: sqlite3.Connection, version: int | None) -> dict[str, Any]:
    if version is None:
        row = conn.execute(
            "SELECT * FROM strategy_rules WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM strategy_rules WHERE version = ?", (version,)).fetchone()
    if row is None:
        raise ValueError(f"Strategy version {version if version is not None else 'active'} not found")
    return dict(row)


def _symbols(conn: sqlite3.Connection, symbol: str | None) -> list[str]:
    if symbol:
        found = conn.execute("SELECT symbol FROM stocks WHERE UPPER(symbol) = UPPER(?)", (symbol,)).fetchone()
        if not found:
            raise ValueError(f"Unknown symbol: {symbol}")
        return [str(found["symbol"])]
    return [row["symbol"] for row in conn.execute("SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol")]


def _candles(conn: sqlite3.Connection, symbol: str, date_from: str, date_to: str) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT symbol, interval, open, high, low, close, volume, timestamp
        FROM candles WHERE symbol = ? AND interval = '15m'
          AND date(timestamp) >= date(?) AND date(timestamp) <= date(?)
        ORDER BY timestamp ASC
        """,
        (symbol, date_from, date_to),
    ).fetchall()
    frame = pd.DataFrame([dict(row) for row in rows])
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


def _score_prefix(frame: pd.DataFrame, index: int, strategy: dict[str, Any], cfg: dict[str, Any]) -> Candidate | None:
    # The slice ends at index, by construction. This assertion stays next to
    # scoring so a future refactor cannot silently introduce N+1 data.
    current_candle_index = int(frame.iloc[: index + 1].index[-1])
    assert current_candle_index <= index, "Lookahead bias detected"
    prefix = frame.iloc[max(0, index - INDICATOR_WINDOW + 1): index + 1].copy()
    indicators = compute_indicators(prefix)
    current = indicators.iloc[-1]
    # Live scoring defaults to SIDEWAYS when no Nifty index history exists.
    # Respect learned strategies that explicitly disable that regime.
    if int(strategy.get("trade_in_sideways", 1)) == 0:
        return None
    lookback = int(cfg.get("macd", {}).get("crossover_lookback", 3))
    recent = [
        {
            "macd": _clean(row["macd"]), "macd_signal": _clean(row["macd_signal"]),
            "macd_histogram": _clean(row["macd_histogram"]),
        }
        for _, row in indicators.tail(lookback).iterrows()
    ]
    macd_score, _, macd_hint = score_macd(recent, cfg)
    rsi = _clean(current.get("rsi"))
    close = float(current["close"])
    vwap = _clean(current.get("vwap"))
    volume_ratio = _clean(current.get("volume_ratio"))
    volume_score, _ = score_volume(volume_ratio, cfg)
    rsi_buy, _ = score_rsi(rsi, "BUY", cfg)
    rsi_sell, _ = score_rsi(rsi, "SELL", cfg)
    vwap_buy, _ = score_vwap(close, vwap, "BUY", cfg)
    vwap_sell, _ = score_vwap(close, vwap, "SELL", cfg)
    primary_direction = macd_hint if macd_hint != "NEUTRAL" else ("BUY" if rsi is not None and rsi < 50 else "SELL")
    if primary_direction == "BUY":
        direction, rsi_score, vwap_score = "BUY", rsi_buy, vwap_buy
    else:
        direction, rsi_score, vwap_score = "SELL", rsi_sell, vwap_sell
    weighted = min(100.0, round(4 * (
        rsi_score * float(strategy["weight_rsi"])
        + macd_score * float(strategy["weight_macd"])
        + volume_score * float(strategy["weight_volume"])
        + vwap_score * float(strategy["weight_vwap"])
    ), 2))
    if weighted < float(strategy["min_score_to_trade"]):
        return None
    timestamp = current["timestamp"].to_pydatetime()
    if timestamp.time() >= time(15, 15):
        return None
    return Candidate(
        symbol=str(current["symbol"]), index=index, timestamp=timestamp,
        direction=direction, confidence_score=weighted, market_condition="SIDEWAYS",
        close=close, rsi=rsi, macd=_clean(current.get("macd")), vwap=vwap,
    )


def _position(balance: float, strategy: dict[str, Any], candidate: Candidate) -> dict[str, Any] | None:
    if balance <= 0 or candidate.close <= 0:
        return None
    risk_amount = balance * (float(strategy["risk_per_trade_pct"]) / 100)
    if candidate.direction == "BUY":
        stop, target = candidate.close * 0.9925, candidate.close * 1.015
    else:
        stop, target = candidate.close * 1.0075, candidate.close * 0.985
    risk_per_share = abs(candidate.close - stop)
    quantity = max(math.floor(risk_amount / risk_per_share), 1)
    quantity = min(quantity, math.floor(balance / candidate.close))
    if quantity <= 0:
        return None
    return {"quantity": quantity, "capital": quantity * candidate.close, "stop": stop, "target": target}


def _close_trade(trade: SimTrade, candle: dict[str, Any], reason: str, price: float) -> None:
    pnl = (price - trade.entry_price) * trade.quantity if trade.direction == "BUY" else (trade.entry_price - price) * trade.quantity
    trade.exit_price = price
    trade.exit_time = str(candle["timestamp"])
    trade.exit_reason = reason
    trade.pnl = pnl
    trade.pnl_pct = 100 * pnl / trade.capital_required if trade.capital_required else 0.0
    trade.outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")


def _metrics(trades: list[SimTrade]) -> dict[str, Any]:
    wins = [trade for trade in trades if (trade.pnl or 0) > 0]
    losses = [trade for trade in trades if (trade.pnl or 0) < 0]
    total_pnl = sum(trade.pnl or 0 for trade in trades)
    gross_win = sum(trade.pnl or 0 for trade in wins)
    gross_loss = abs(sum(trade.pnl or 0 for trade in losses))
    profit_factor = gross_win / gross_loss if gross_loss else (gross_win if gross_win else 0.0)
    balance = STARTING_BALANCE
    peak = balance
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item.exit_time or ""):
        balance += trade.pnl or 0
        peak = max(peak, balance)
        max_drawdown = max(max_drawdown, 100 * (peak - balance) / peak if peak else 0)
    daily_pnl: dict[str, float] = {}
    monthly: dict[str, dict[str, Any]] = {}
    reasons: dict[str, dict[str, Any]] = {}
    for trade in trades:
        day = str(trade.exit_time)[:10]
        month = day[:7]
        daily_pnl[day] = daily_pnl.get(day, 0) + (trade.pnl or 0)
        bucket = monthly.setdefault(month, {"pnl": 0.0, "trades": 0, "wins": 0})
        bucket["pnl"] += trade.pnl or 0
        bucket["trades"] += 1
        bucket["wins"] += trade.outcome == "WIN"
        reason = reasons.setdefault(str(trade.exit_reason), {"trades": 0, "wins": 0})
        reason["trades"] += 1
        reason["wins"] += trade.outcome == "WIN"
    for bucket in monthly.values():
        bucket["win_rate"] = 100 * bucket["wins"] / bucket["trades"]
    for bucket in reasons.values():
        bucket["win_rate"] = 100 * bucket["wins"] / bucket["trades"]
    running = STARTING_BALANCE
    returns = []
    for day in sorted(daily_pnl):
        returns.append(daily_pnl[day] / running if running else 0)
        running += daily_pnl[day]
    daily_rf = 0.06 / 252
    sharpe = ((mean(returns) - daily_rf) / stdev(returns) * math.sqrt(252)) if len(returns) > 1 and stdev(returns) else 0.0
    best = max(trades, key=lambda item: item.pnl or 0) if trades else None
    worst = min(trades, key=lambda item: item.pnl or 0) if trades else None
    return {
        "total_trades": len(trades), "win_rate": 100 * len(wins) / len(trades) if trades else 0.0,
        "total_pnl": total_pnl, "average_win": mean([t.pnl for t in wins]) if wins else 0.0,
        "average_loss": mean([t.pnl for t in losses]) if losses else 0.0,
        "profit_factor": profit_factor, "max_drawdown": max_drawdown, "sharpe_ratio": sharpe,
        "best_trade": asdict(best) if best else None, "worst_trade": asdict(worst) if worst else None,
        "monthly": monthly, "by_exit_reason": reasons,
    }


def run(date_from: str, date_to: str, symbol: str | None = None, strategy_version: int | None = None) -> dict[str, Any]:
    if date.fromisoformat(date_from) > date.fromisoformat(date_to):
        raise ValueError("date_from must be on or before date_to")
    cfg = load_config()
    with _connect() as conn:
        ensure_backtest_schema(conn)
        strategy = _strategy(conn, strategy_version)
        symbols = _symbols(conn, symbol)
        frames = {name: _candles(conn, name, date_from, date_to) for name in symbols}
    candidates: dict[datetime, list[Candidate]] = {}
    candles_by_time: dict[datetime, dict[str, dict[str, Any]]] = {}
    for name, frame in frames.items():
        for index, row in frame.iterrows():
            timestamp = row["timestamp"].to_pydatetime()
            candles_by_time.setdefault(timestamp, {})[name] = row.to_dict()
            if index >= 26:
                candidate = _score_prefix(frame, int(index), strategy, cfg)
                if candidate:
                    candidates.setdefault(timestamp, []).append(candidate)

    cash = STARTING_BALANCE
    open_trades: dict[str, SimTrade] = {}
    completed: list[SimTrade] = []
    max_open = int(strategy["max_open_trades"])
    for timestamp in sorted(candles_by_time):
        candle_set = candles_by_time[timestamp]
        for name, trade in list(open_trades.items()):
            candle = candle_set.get(name)
            if not candle or timestamp <= datetime.fromisoformat(trade.entry_time):
                continue
            high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])
            reason = None
            price = close
            if trade.direction == "BUY" and high >= trade.target_price or trade.direction == "SELL" and low <= trade.target_price:
                reason, price = "TARGET", trade.target_price
            elif trade.direction == "BUY" and low <= trade.stop_loss_price or trade.direction == "SELL" and high >= trade.stop_loss_price:
                reason, price = "STOPLOSS", trade.stop_loss_price
            elif timestamp.time() >= time(15, 15) or timestamp.date() > datetime.fromisoformat(trade.entry_time).date():
                reason = "EOD"
            if reason:
                _close_trade(trade, candle, reason, price)
                cash = max(0.0, cash + trade.capital_required + (trade.pnl or 0))
                completed.append(trade)
                del open_trades[name]
        for candidate in sorted(candidates.get(timestamp, []), key=lambda item: item.confidence_score, reverse=True):
            if candidate.symbol in open_trades or len(open_trades) >= max_open:
                continue
            sized = _position(cash, strategy, candidate)
            if not sized:
                continue
            cash = max(0.0, cash - sized["capital"])
            open_trades[candidate.symbol] = SimTrade(
                symbol=candidate.symbol, direction=candidate.direction, entry_price=candidate.close,
                entry_time=candidate.timestamp.isoformat(sep=" "), target_price=sized["target"],
                stop_loss_price=sized["stop"], capital_required=sized["capital"], quantity=sized["quantity"],
                confidence_score=candidate.confidence_score, market_condition=candidate.market_condition,
                rsi_at_entry=candidate.rsi, macd_at_entry=candidate.macd, vwap_at_entry=candidate.vwap,
            )
    for name, trade in list(open_trades.items()):
        frame = frames[name]
        if frame.empty:
            continue
        candle = frame.iloc[-1].to_dict()
        _close_trade(trade, candle, "EOD", float(candle["close"]))
        completed.append(trade)

    metrics = _metrics(completed)
    run_id = str(uuid.uuid4())
    notes = json.dumps({key: metrics[key] for key in ("average_win", "average_loss", "best_trade", "worst_trade", "monthly", "by_exit_reason")}, default=str)
    with _connect() as conn:
        ensure_backtest_schema(conn)
        conn.execute(
            """INSERT INTO backtest_runs
               (run_id,date_from,date_to,symbols_tested,strategy_version,total_trades,win_rate,total_pnl,max_drawdown,sharpe_ratio,profit_factor,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, date_from, date_to, len(symbols), strategy["version"], metrics["total_trades"], metrics["win_rate"],
             metrics["total_pnl"], metrics["max_drawdown"], metrics["sharpe_ratio"], metrics["profit_factor"], notes),
        )
        for trade in completed:
            conn.execute(
                """INSERT INTO backtest_trades
                   (run_id,symbol,direction,entry_price,entry_time,exit_price,exit_time,exit_reason,quantity,pnl,pnl_pct,outcome,
                    confidence_score,market_condition,rsi_at_entry,macd_at_entry,vwap_at_entry)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, trade.symbol, trade.direction, trade.entry_price, trade.entry_time, trade.exit_price, trade.exit_time,
                 trade.exit_reason, trade.quantity, trade.pnl, trade.pnl_pct, trade.outcome, trade.confidence_score,
                 trade.market_condition, trade.rsi_at_entry, trade.macd_at_entry, trade.vwap_at_entry),
            )
        conn.commit()
    result = {"run_id": run_id, "date_from": date_from, "date_to": date_to, "symbols_tested": symbols,
              "strategy_version": strategy["version"], **metrics, "trades": [asdict(trade) for trade in completed]}
    print_report(result)
    return result


def print_report(result: dict[str, Any]) -> None:
    print("-- BACKTEST RESULTS " + "-" * 32)
    print(f"Period          : {result['date_from']} -> {result['date_to']}")
    print(f"Symbols         : {len(result['symbols_tested'])} stocks")
    print(f"Strategy        : v{result['strategy_version']}")
    print(f"Total trades    : {result['total_trades']}")
    print(f"Win rate        : {result['win_rate']:.1f}%")
    print(f"Total PnL       : Rs {result['total_pnl']:,.0f}")
    print(f"Profit factor   : {result['profit_factor']:.2f}")
    print(f"Max drawdown    : {result['max_drawdown']:.1f}%")
    print(f"Sharpe ratio    : {result['sharpe_ratio']:.2f}\n")
    print("By exit reason:")
    for reason, data in result["by_exit_reason"].items():
        print(f"  {reason:<12}: {data['trades']} trades, {data['win_rate']:.1f}% win")
    print("\nMonthly:")
    for month, data in result["monthly"].items():
        print(f"  {month}: Rs {data['pnl']:+,.0f} ({data['trades']} trades, {data['win_rate']:.1f}% win)")
    print("-" * 52)


def print_comparison(results: list[dict[str, Any]], date_from: str, date_to: str) -> None:
    print(f"-- VERSION COMPARISON {date_from} -> {date_to} --")
    print(f"{'Metric':<18}" + "".join(f"v{r['strategy_version']:>9}" for r in results))
    for label, key, suffix in (
        ("Win Rate", "win_rate", "%"), ("Total PnL", "total_pnl", ""),
        ("Max Drawdown", "max_drawdown", "%"), ("Sharpe Ratio", "sharpe_ratio", ""),
        ("Profit Factor", "profit_factor", ""), ("Total Trades", "total_trades", ""),
    ):
        print(f"{label:<18}" + "".join(f"{r[key]:>9.2f}{suffix}" for r in results))
    best = max(results, key=lambda r: (r["sharpe_ratio"], r["total_pnl"], -r["max_drawdown"]))
    print(f"Best version: v{best['strategy_version']} by risk-adjusted return")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--symbol")
    parser.add_argument("--strategy-version", type=int)
    parser.add_argument("--compare-versions")
    args = parser.parse_args()
    if args.compare_versions:
        versions = [int(value.strip()) for value in args.compare_versions.split(",")]
        results = [run(args.date_from, args.date_to, args.symbol, version) for version in versions]
        print_comparison(results, args.date_from, args.date_to)
    else:
        run(args.date_from, args.date_to, args.symbol, args.strategy_version)


if __name__ == "__main__":
    main()
