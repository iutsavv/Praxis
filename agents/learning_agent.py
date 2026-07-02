"""Learn from closed paper trades and version the active strategy.

This module only reads CLOSED trades and writes strategy_rules and
learning_insights. It never opens, closes, or modifies a paper trade.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytz


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = pytz.timezone("Asia/Kolkata")
MIN_TRADES = 20
SIGNALS = ("rsi", "macd", "volume", "vwap")
logger = logging.getLogger("learning_agent")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_learning_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations needed by learning; existing data is preserved."""
    strategy_additions = {
        "trade_in_sideways": "INTEGER NOT NULL DEFAULT 1",
        "notes": "TEXT",
    }
    insight_additions = {
        "trades_analyzed": "INTEGER",
        "strategy_version": "INTEGER",
    }
    trade_additions = {
        "rsi_score": "REAL",
        "macd_score": "REAL",
        "volume_score": "REAL",
        "vwap_score": "REAL",
    }
    for table, additions in (
        ("strategy_rules", strategy_additions),
        ("learning_insights", insight_additions),
        ("paper_trades", trade_additions),
    ):
        existing = _columns(conn, table)
        for name, definition in additions.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.commit()


def _bounded_normalize(raw: list[float], old: list[float]) -> list[float]:
    """Project weights to sum 1 while honoring global and per-cycle bounds."""
    lower = [max(0.05, value - 0.05) for value in old]
    upper = [min(0.50, value + 0.05) for value in old]
    values = [min(max(value, lo), hi) for value, lo, hi in zip(raw, lower, upper)]
    for _ in range(20):
        difference = 1.0 - sum(values)
        if abs(difference) < 1e-12:
            break
        candidates = [
            index for index, value in enumerate(values)
            if (difference > 0 and value < upper[index] - 1e-12)
            or (difference < 0 and value > lower[index] + 1e-12)
        ]
        if not candidates:
            raise RuntimeError("Weight bounds cannot be normalized to 1.0")
        share = difference / len(candidates)
        for index in candidates:
            values[index] = min(max(values[index] + share, lower[index]), upper[index])
    values[-1] += 1.0 - sum(values)
    return values


def _signal_analysis(trades: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    analysis: dict[str, dict[str, Any]] = {}
    for signal_name in SIGNALS:
        column = f"{signal_name}_score"
        wins = [float(row[column]) for row in trades if row["outcome"] == "WIN" and row[column] is not None]
        losses = [float(row[column]) for row in trades if row["outcome"] == "LOSS" and row[column] is not None]
        win_average = sum(wins) / len(wins) if wins else None
        loss_average = sum(losses) / len(losses) if losses else None
        predictive_power = win_average - loss_average if win_average is not None and loss_average is not None else None
        delta = 0.0 if predictive_power is None or 5 <= predictive_power <= 15 else (0.05 if predictive_power > 15 else -0.05)
        analysis[signal_name] = {
            "win_average": win_average,
            "loss_average": loss_average,
            "predictive_power": predictive_power,
            "win_samples": len(wins),
            "loss_samples": len(losses),
            "requested_delta": delta,
        }
    return analysis


def _time_analysis(trades: list[sqlite3.Row]) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    hours: dict[int, list[sqlite3.Row]] = {}
    for row in trades:
        try:
            hour = datetime.fromisoformat(str(row["entry_time"])).hour
        except ValueError:
            continue
        hours.setdefault(hour, []).append(row)
    hourly = {
        f"{hour:02d}:00": {
            "trades": len(rows),
            "win_rate": 100 * sum(r["outcome"] == "WIN" for r in rows) / len(rows),
        }
        for hour, rows in sorted(hours.items()) if len(rows) >= 5
    }
    candidates: list[tuple[float, int, int]] = []
    for hour in sorted(hours):
        if len(hours[hour]) < 5 or len(hours.get(hour + 1, [])) < 5:
            continue
        combined = hours[hour] + hours[hour + 1]
        win_rate = 100 * sum(row["outcome"] == "WIN" for row in combined) / len(combined)
        candidates.append((win_rate, len(combined), hour))
    if not candidates:
        return None, None, hourly
    win_rate, count, hour = max(candidates, key=lambda item: (item[0], item[1]))
    window = f"{hour:02d}:00-{hour + 2:02d}:00"
    return window, {"win_rate": win_rate, "trades": count}, hourly


def _market_analysis(trades: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for condition in ("TRENDING", "SIDEWAYS", "VOLATILE"):
        rows = [row for row in trades if str(row["market_condition"] or "").upper() == condition]
        result[condition] = {
            "trades": len(rows),
            "win_rate": 100 * sum(row["outcome"] == "WIN" for row in rows) / len(rows) if rows else None,
        }
    return result


def _score_analysis(trades: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    ranges = {"60-70": (60, 70), "70-80": (70, 80), "80-100": (80, 101)}
    result: dict[str, dict[str, Any]] = {}
    for label, (low, high) in ranges.items():
        rows = [row for row in trades if row["confidence_score"] is not None and low <= float(row["confidence_score"]) < high]
        result[label] = {
            "trades": len(rows),
            "win_rate": 100 * sum(row["outcome"] == "WIN" for row in rows) / len(rows) if rows else None,
        }
    return result


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def run_learning_cycle(force: bool = False) -> dict[str, Any]:
    conn = _connect()
    try:
        ensure_learning_schema(conn)
        trades = conn.execute("SELECT * FROM paper_trades WHERE status = 'CLOSED' ORDER BY entry_time").fetchall()
        trade_count = len(trades)
        if trade_count < MIN_TRADES and not force:
            message = f"Not enough data yet ({trade_count}/{MIN_TRADES} closed trades). Skipping learning cycle."
            logger.info(message)
            print(message)
            return {"skipped": True, "reason": message, "trades_analyzed": trade_count}

        current = conn.execute(
            "SELECT * FROM strategy_rules WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if current is None:
            raise RuntimeError("No active strategy_rules row found")

        signals = _signal_analysis(trades)
        old_weights = [float(current[f"weight_{name}"]) for name in SIGNALS]
        raw_weights = [old + signals[name]["requested_delta"] for name, old in zip(SIGNALS, old_weights)]
        new_weights = _bounded_normalize(raw_weights, old_weights)
        for name, old, new in zip(SIGNALS, old_weights, new_weights):
            signals[name]["old_weight"] = old
            signals[name]["new_weight"] = new
            signals[name]["actual_delta"] = new - old

        best_window, best_window_stats, hourly = _time_analysis(trades)
        market = _market_analysis(trades)
        sideways = market["SIDEWAYS"]
        trade_in_sideways = int(current["trade_in_sideways"])
        sideways_change = None
        if sideways["trades"] >= 10 and sideways["win_rate"] is not None:
            if sideways["win_rate"] < 35:
                trade_in_sideways, sideways_change = 0, "disabled"
            elif sideways["win_rate"] > 50:
                trade_in_sideways, sideways_change = 1, "enabled"

        scores = _score_analysis(trades)
        old_min_score = float(current["min_score_to_trade"])
        new_min_score = old_min_score
        threshold_reason = None
        low, high = scores["60-70"], scores["80-100"]
        if low["trades"] and low["win_rate"] < 40:
            new_min_score += 5
            threshold_reason = f"60-70 range had {low['win_rate']:.1f}% win rate"
        elif high["trades"] and high["trades"] < 5 and high["win_rate"] > 65:
            new_min_score -= 2
            threshold_reason = f"80-100 range had {high['win_rate']:.1f}% win rate but only {high['trades']} trades"
        new_min_score = min(85, max(50, new_min_score))

        wins = sum(row["outcome"] == "WIN" for row in trades)
        overall_win_rate = 100 * wins / trade_count if trade_count else 0.0
        eligible_market = [(name, data) for name, data in market.items() if data["trades"]]
        best_market = max(eligible_market, key=lambda item: item[1]["win_rate"])[0] if eligible_market else None
        predictive = [(name, data["predictive_power"]) for name, data in signals.items() if data["predictive_power"] is not None]
        worst_signal = min(predictive, key=lambda item: item[1])[0].upper() if predictive else None
        new_version = int(current["version"]) + 1

        notes: list[str] = []
        date_text = datetime.now(IST).date().isoformat()
        for name in SIGNALS:
            data = signals[name]
            if abs(data["actual_delta"]) > 1e-8:
                notes.append(
                    f"{name.upper()} weight {data['old_weight']:.2f}->{data['new_weight']:.2f} "
                    f"(predictive power: {_fmt(data['predictive_power'])})."
                )
        if sideways_change:
            notes.append(
                f"Sideways trading {sideways_change} (win rate {_fmt(sideways['win_rate'])}, {sideways['trades']} trades)."
            )
        if best_window and best_window_stats:
            notes.append(
                f"Best entry window: {best_window} ({best_window_stats['win_rate']:.1f}% win rate, "
                f"{best_window_stats['trades']} trades)."
            )
        if new_min_score != old_min_score:
            direction = "raised" if new_min_score > old_min_score else "lowered"
            notes.append(f"Min score {direction}: {old_min_score:g}->{new_min_score:g} ({threshold_reason}).")
        notes.append(f"Analyzed {trade_count} trades total.")
        notes_text = f"v{new_version} ({date_text}): " + "\n".join(notes)

        findings = {
            "signals": signals,
            "time_of_day": {"hourly": hourly, "best_window": best_window, "best_window_stats": best_window_stats},
            "market_conditions": market,
            "score_ranges": scores,
            "overall_win_rate": overall_win_rate,
            "trades_analyzed": trade_count,
            "changes": {
                "trade_in_sideways": {"old": int(current["trade_in_sideways"]), "new": trade_in_sideways},
                "min_score_to_trade": {"old": old_min_score, "new": new_min_score},
                "best_entry_window": {"old": current["best_entry_window"], "new": best_window or current["best_entry_window"]},
            },
        }

        strategy_columns = _columns(conn, "strategy_rules")
        copy_columns = [
            name for name in strategy_columns
            if name not in {"id", "version", "is_active", "created_at", "updated_at"}
        ]
        strategy_values = dict(current)
        strategy_values.update({
            "weight_rsi": new_weights[0], "weight_macd": new_weights[1],
            "weight_volume": new_weights[2], "weight_vwap": new_weights[3],
            "min_score_to_trade": new_min_score,
            "best_entry_window": best_window or current["best_entry_window"],
            "trade_in_sideways": trade_in_sideways, "notes": notes_text,
        })
        conn.execute("BEGIN")
        conn.execute("UPDATE strategy_rules SET is_active = 0 WHERE is_active = 1")
        insert_columns = ["version", "is_active", *copy_columns]
        conn.execute(
            f"INSERT INTO strategy_rules ({', '.join(insert_columns)}) VALUES ({', '.join('?' for _ in insert_columns)})",
            [new_version, 1, *(strategy_values[name] for name in copy_columns)],
        )
        today = datetime.now(IST).date()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=4)
        conn.execute(
            """
            INSERT INTO learning_insights (
                week_start, week_end, total_trades, trades_analyzed, strategy_version,
                win_rate, best_time_window, best_market_condition, worst_signal,
                findings_json, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                week_start.isoformat(), week_end.isoformat(), trade_count, trade_count, new_version,
                overall_win_rate, best_window, best_market, worst_signal,
                json.dumps(findings, default=str), notes_text,
            ),
        )
        conn.commit()

        lines = [
            f"-- LEARNING CYCLE v{current['version']} -> v{new_version} " + "-" * 24,
            f"Trades analyzed : {trade_count}",
            f"Overall win rate: {overall_win_rate:.1f}%", "", "Signal changes:",
        ]
        for name in SIGNALS:
            data = signals[name]
            lines.append(
                f"  {name.upper():7}: {data['old_weight']:.2f} -> {data['new_weight']:.2f} "
                f"({data['actual_delta']:+.2f}) predictive power: {_fmt(data['predictive_power'])}"
            )
        lines.extend([
            "",
            f"Best entry window : {best_window or 'Not enough hourly data'}" +
            (f" ({best_window_stats['win_rate']:.1f}% win rate)" if best_window_stats else ""),
            f"Sideways trading  : {'ENABLED' if trade_in_sideways else 'DISABLED'} "
            f"({_fmt(sideways['win_rate'])}, {sideways['trades']} trades)",
            f"Min score         : {old_min_score:g} -> {new_min_score:g}",
            "-" * 52,
        ])
        report = "\n".join(lines)
        print(report)
        return {
            "skipped": False,
            "new_strategy_version": new_version,
            "what_changed": notes_text,
            "findings": findings,
            "report": report,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Learn from closed paper trades")
    parser.add_argument("--run-once", action="store_true", help="Run one learning cycle")
    parser.add_argument("--force", action="store_true", help="Bypass the 20-closed-trade prerequisite")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not args.run_once:
        parser.error("--run-once is required when invoking this module directly")
    run_learning_cycle(force=args.force)


if __name__ == "__main__":
    main()
