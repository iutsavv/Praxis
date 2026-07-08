"""Learn from closed paper trades and version the active strategy.

This module only reads CLOSED trades and writes strategy_rules and
learning_insights. It never opens, closes, or modifies a paper trade.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pytz

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
IST = pytz.timezone("Asia/Kolkata")
SIGNALS = ("rsi", "macd", "volume", "vwap")
logger = logging.getLogger("learning_agent")

MINIMUM_SAMPLES = {
    'signal_weights': 20,
    'threshold': 50,
    'max_trades': 30,
    'sideways_filter': 40,
    'time_window': 25,
}

THRESHOLD_FLOOR = 40
THRESHOLD_CEILING = 65


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_learning_schema(conn: sqlite3.Connection) -> None:
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


def find_optimal_threshold(closed_trades: list[sqlite3.Row]) -> float | None:
    if len(closed_trades) < MINIMUM_SAMPLES['threshold']:
        return None

    buckets = {}
    for trade in closed_trades:
        if trade['confidence_score'] is None:
            continue
        bucket = (float(trade['confidence_score']) // 5) * 5
        if bucket not in buckets:
            buckets[bucket] = {'wins': 0, 'total': 0}
        buckets[bucket]['total'] += 1
        if trade['outcome'] == 'WIN':
            buckets[bucket]['wins'] += 1

    valid_buckets = {k: v for k, v in buckets.items() if v['total'] >= 5}
    if not valid_buckets:
        return None

    profitable_buckets = [
        k for k, v in valid_buckets.items()
        if v['wins'] / v['total'] >= 0.50
    ]

    if not profitable_buckets:
        return None

    natural_threshold = min(profitable_buckets)
    return max(THRESHOLD_FLOOR, min(THRESHOLD_CEILING, natural_threshold))


def get_last_threshold_change(conn: sqlite3.Connection) -> dict | None:
    insights = conn.execute("SELECT strategy_version, findings_json FROM learning_insights ORDER BY strategy_version DESC").fetchall()
    for row in insights:
        if not row["findings_json"]:
            continue
        try:
            data = json.loads(row["findings_json"])
            if "changes" in data and "min_score_to_trade" in data["changes"]:
                old = data["changes"]["min_score_to_trade"]["old"]
                new = data["changes"]["min_score_to_trade"]["new"]
                if old != new:
                    return {"cycle_number": row["strategy_version"]}
        except Exception:
            pass
    return None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def run_learning_cycle(force: bool = False) -> dict[str, Any]:
    conn = _connect()
    try:
        ensure_learning_schema(conn)
        trades = conn.execute("SELECT * FROM paper_trades WHERE status = 'CLOSED' ORDER BY entry_time").fetchall()
        trade_count = len(trades)
        
        current = conn.execute(
            "SELECT * FROM strategy_rules WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if current is None:
            raise RuntimeError("No active strategy_rules row found")
            
        current_cycle = int(current["version"])

        findings = {
            "adjustments": {}
        }
        report_lines = [
            f"── LEARNING CYCLE v{current_cycle} → v{current_cycle + 1} " + "─" * 22,
            f"Trades analyzed: {trade_count} total",
            ""
        ]
        
        # 1. Signal Weights
        signals = _signal_analysis(trades)
        old_weights = [float(current[f"weight_{name}"]) for name in SIGNALS]
        new_weights = old_weights[:]
        
        if trade_count < MINIMUM_SAMPLES['signal_weights'] and not force:
            findings["adjustments"]["signal_weights"] = {
                "status": "SKIPPED", "samples": trade_count, "required": MINIMUM_SAMPLES['signal_weights'],
                "reason": f"need {MINIMUM_SAMPLES['signal_weights']} trades, have {trade_count}"
            }
            report_lines.append(f"SIGNAL WEIGHTS (need {MINIMUM_SAMPLES['signal_weights']}, have {trade_count} ❌):")
            report_lines.append(f"  SKIPPED — need {MINIMUM_SAMPLES['signal_weights']} trades")
        else:
            raw_weights = [old + signals[name]["requested_delta"] for name, old in zip(SIGNALS, old_weights)]
            new_weights = _bounded_normalize(raw_weights, old_weights)
            findings["adjustments"]["signal_weights"] = {
                "status": "FIRED" if new_weights != old_weights else "SKIPPED",
                "samples": trade_count,
                "reason": "calculated optimal weights" if new_weights != old_weights else "no change needed"
            }
            report_lines.append(f"SIGNAL WEIGHTS (need {MINIMUM_SAMPLES['signal_weights']}, have {trade_count} ✅):")
            for name, old, new in zip(SIGNALS, old_weights, new_weights):
                signals[name]["old_weight"] = old
                signals[name]["new_weight"] = new
                signals[name]["actual_delta"] = new - old
                power = _fmt(signals[name]['predictive_power'])
                change_str = " (no change)" if abs(new - old) < 1e-8 else ""
                report_lines.append(f"  {name.upper():6} : {old:.2f} → {new:.2f}  predictive power {power}{change_str}")

        report_lines.append("")

        # 2. Threshold
        old_min_score = float(current["min_score_to_trade"])
        new_min_score = old_min_score
        
        bucket_50_65 = [t for t in trades if t['confidence_score'] is not None and 50 <= float(t['confidence_score']) < 65]
        bucket_count = len(bucket_50_65)
        
        last_change = get_last_threshold_change(conn)
        cycles_since_change = (current_cycle - last_change['cycle_number']) if last_change else 999
        
        if bucket_count < 15:
            findings["adjustments"]["threshold"] = {
                "status": "SKIPPED", "samples": bucket_count, "required": 15,
                "reason": f"only {bucket_count} trades scored 50-65"
            }
            report_lines.append(f"THRESHOLD (need 15 in bucket, have {bucket_count} ❌):")
            report_lines.append(f"  SKIPPED — only {bucket_count} trades scored 50-65")
            report_lines.append(f"  Keeping threshold at {old_min_score:g}")
            report_lines.append("  Will reassess when bucket has 15+ trades")
        elif cycles_since_change < 2:
            findings["adjustments"]["threshold"] = {
                "status": "SKIPPED", "samples": bucket_count, "required": 15,
                "reason": f"changed {cycles_since_change} cycles ago (need 2 stable cycles)"
            }
            report_lines.append(f"THRESHOLD (cooldown active ❌):")
            report_lines.append(f"  SKIPPED — Threshold changed {cycles_since_change} cycle(s) ago.")
            report_lines.append("  Waiting for 2 stable cycles before changing again.")
            report_lines.append(f"  Keeping threshold at {old_min_score:g}")
        else:
            calc_threshold = find_optimal_threshold(trades)
            if calc_threshold is None:
                findings["adjustments"]["threshold"] = {
                    "status": "SKIPPED", "samples": trade_count, "required": MINIMUM_SAMPLES['threshold'],
                    "reason": "could not find profitable bucket"
                }
                report_lines.append(f"THRESHOLD (need {MINIMUM_SAMPLES['threshold']} total, have {trade_count} ❌):")
                report_lines.append("  SKIPPED — could not find natural threshold (no bucket with >= 50% win rate).")
            else:
                new_min_score = calc_threshold
                if new_min_score > THRESHOLD_CEILING:
                    report_lines.append(f"  WARNING: Learning agent wanted threshold={new_min_score} but capped at {THRESHOLD_CEILING}")
                    report_lines.append("  Manual review recommended — strategy may be underperforming")
                    new_min_score = THRESHOLD_CEILING
                findings["adjustments"]["threshold"] = {
                    "status": "FIRED" if new_min_score != old_min_score else "SKIPPED",
                    "samples": bucket_count,
                    "reason": "adjusted to natural threshold" if new_min_score != old_min_score else "no change needed"
                }
                report_lines.append(f"THRESHOLD (need 15 in bucket, have {bucket_count} ✅):")
                if new_min_score != old_min_score:
                    report_lines.append(f"  Calculated natural threshold: {calc_threshold}")
                    report_lines.append(f"  Adjusted: {old_min_score:g} → {new_min_score:g}")
                else:
                    report_lines.append(f"  Calculated natural threshold is same as current: {new_min_score:g}")
        
        report_lines.append("")
        
        # 3. Max Trades
        old_max_trades = int(current["max_open_trades"])
        new_max_trades = old_max_trades
        if trade_count < MINIMUM_SAMPLES['max_trades'] and not force:
            findings["adjustments"]["max_trades"] = {
                "status": "SKIPPED", "samples": trade_count, "required": MINIMUM_SAMPLES['max_trades'],
                "reason": f"need {MINIMUM_SAMPLES['max_trades']} trades, have {trade_count}"
            }
            report_lines.append(f"MAX TRADES (need {MINIMUM_SAMPLES['max_trades']}, have {trade_count} ❌):")
            report_lines.append(f"  SKIPPED — need {MINIMUM_SAMPLES['max_trades']} trades, have {trade_count}")
        else:
            wins = sum(row["outcome"] == "WIN" for row in trades)
            win_rate = 100 * wins / trade_count if trade_count else 0.0
            if win_rate > 60:
                new_max_trades = min(5, old_max_trades + 1)
            elif win_rate < 40:
                new_max_trades = max(1, old_max_trades - 1)
            findings["adjustments"]["max_trades"] = {
                "status": "FIRED" if new_max_trades != old_max_trades else "SKIPPED",
                "samples": trade_count,
                "reason": f"win rate {win_rate:.0f}%"
            }
            report_lines.append(f"MAX TRADES (need {MINIMUM_SAMPLES['max_trades']}, have {trade_count} ✅):")
            if new_max_trades != old_max_trades:
                report_lines.append(f"  Recent win rate: {win_rate:.0f}% → adjusted max trades {old_max_trades} → {new_max_trades}")
            else:
                report_lines.append(f"  Recent win rate: {win_rate:.0f}% → no change to max trades")
                
        report_lines.append("")

        # 4. Sideways Filter
        market = _market_analysis(trades)
        sideways = market.get("SIDEWAYS", {"trades": 0, "win_rate": None})
        old_trade_in_sideways = int(current["trade_in_sideways"])
        new_trade_in_sideways = old_trade_in_sideways
        
        if trade_count < MINIMUM_SAMPLES['sideways_filter'] and not force:
            findings["adjustments"]["sideways_filter"] = {
                "status": "SKIPPED", "samples": trade_count, "required": MINIMUM_SAMPLES['sideways_filter'],
                "reason": f"need {MINIMUM_SAMPLES['sideways_filter']} trades, have {trade_count}"
            }
            report_lines.append(f"SIDEWAYS FILTER (need {MINIMUM_SAMPLES['sideways_filter']}, have {trade_count} ❌):")
            report_lines.append(f"  SKIPPED — need {MINIMUM_SAMPLES['sideways_filter']} trades, have {trade_count}")
        else:
            if sideways["trades"] >= 10 and sideways["win_rate"] is not None:
                if sideways["win_rate"] < 35:
                    new_trade_in_sideways = 0
                elif sideways["win_rate"] > 50:
                    new_trade_in_sideways = 1
            findings["adjustments"]["sideways_filter"] = {
                "status": "FIRED" if new_trade_in_sideways != old_trade_in_sideways else "SKIPPED",
                "samples": trade_count,
                "reason": f"win rate {sideways['win_rate']}%" if sideways['win_rate'] is not None else "no change needed"
            }
            report_lines.append(f"SIDEWAYS FILTER (need {MINIMUM_SAMPLES['sideways_filter']}, have {trade_count} ✅):")
            if new_trade_in_sideways != old_trade_in_sideways:
                action = "disabled" if new_trade_in_sideways == 0 else "enabled"
                report_lines.append(f"  {action.capitalize()} sideways trading (win rate {_fmt(sideways['win_rate'])})")
            else:
                report_lines.append(f"  No change to sideways trading (win rate {_fmt(sideways['win_rate'])})")
                
        report_lines.append("")

        # 5. Time Window
        best_window, best_window_stats, hourly = _time_analysis(trades)
        old_window = current["best_entry_window"]
        new_window = best_window or old_window
        
        if trade_count < MINIMUM_SAMPLES['time_window'] and not force:
            findings["adjustments"]["time_window"] = {
                "status": "SKIPPED", "samples": trade_count, "required": MINIMUM_SAMPLES['time_window'],
                "reason": f"need {MINIMUM_SAMPLES['time_window']} trades, have {trade_count}"
            }
            report_lines.append(f"TIME WINDOW (need {MINIMUM_SAMPLES['time_window']}, have {trade_count} ❌):")
            report_lines.append(f"  SKIPPED — need {MINIMUM_SAMPLES['time_window']} trades, have {trade_count}")
        else:
            findings["adjustments"]["time_window"] = {
                "status": "FIRED" if new_window != old_window and best_window else "SKIPPED",
                "samples": trade_count,
                "reason": "found better window" if new_window != old_window and best_window else "no change needed"
            }
            report_lines.append(f"TIME WINDOW (need {MINIMUM_SAMPLES['time_window']}, have {trade_count} ✅):")
            if best_window and best_window_stats:
                if new_window != old_window:
                    report_lines.append(f"  Best window: {best_window} ({best_window_stats['win_rate']:.0f}% win rate, {best_window_stats['trades']} trades)")
                    report_lines.append(f"  Updated best_entry_window: {old_window} → {new_window}")
                else:
                    report_lines.append(f"  Best window remains {best_window} ({best_window_stats['win_rate']:.0f}% win rate, {best_window_stats['trades']} trades)")
            else:
                report_lines.append(f"  Not enough hourly data to determine best window")

        report_lines.append("")
        new_version = current_cycle + 1
        report_lines.append(f"Strategy version: {current_cycle} → {new_version}")
        report_lines.append("────────────────────────────────────────────────")

        notes_text = f"Learning Cycle v{current_cycle} -> v{new_version} applied."

        wins = sum(row["outcome"] == "WIN" for row in trades)
        overall_win_rate = 100 * wins / trade_count if trade_count else 0.0
        eligible_market = [(name, data) for name, data in market.items() if data["trades"]]
        best_market = max(eligible_market, key=lambda item: item[1]["win_rate"])[0] if eligible_market else None
        predictive = [(name, data["predictive_power"]) for name, data in signals.items() if data["predictive_power"] is not None]
        worst_signal = min(predictive, key=lambda item: item[1])[0].upper() if predictive else None

        findings.update({
            "signals": signals,
            "time_of_day": {"hourly": hourly, "best_window": best_window, "best_window_stats": best_window_stats},
            "market_conditions": market,
            "overall_win_rate": overall_win_rate,
            "trades_analyzed": trade_count,
            "changes": {
                "trade_in_sideways": {"old": old_trade_in_sideways, "new": new_trade_in_sideways},
                "min_score_to_trade": {"old": old_min_score, "new": new_min_score},
                "max_open_trades": {"old": old_max_trades, "new": new_max_trades},
                "best_entry_window": {"old": old_window, "new": new_window},
            },
        })

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
            "max_open_trades": new_max_trades,
            "best_entry_window": new_window,
            "trade_in_sideways": new_trade_in_sideways, "notes": notes_text,
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
                json.dumps(findings, default=str), "\n".join(report_lines),
            ),
        )
        conn.commit()

        report = "\n".join(report_lines)
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
