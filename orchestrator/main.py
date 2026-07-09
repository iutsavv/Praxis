# RECOMMENDED FIRST RUN ORDER:
# Day 1: python orchestrator/main.py --run-once --dry-run
#   Verify the logs look correct, no import errors, pipeline flows
# Day 2-3: python orchestrator/main.py --dry-run
#   Let it run during full market hours, watch logs, confirm scan
#   behavior is sensible across a real trading day
# Day 4+: python orchestrator/main.py
#   Real paper trading begins

"""Schedule and sequence the already-tested paper-trading pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.paper_trader import monitor_open_trades, open_trade, get_open_trades_with_unrealized_pnl, close_trade
from agents.learning_agent import run_learning_cycle
from agents.performance_tracker import update_stats
from agents.risk_manager import calculate_position
from agents.trade_validator import validate_signal
from analysis.indicator_engine import run_indicator_engine
from analysis.signal_engine import run_scan
from data.fetcher import run_fetcher
from data.universe_fetcher import update_universe


IST = pytz.timezone("Asia/Kolkata")
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
LOG_DIR = PROJECT_ROOT / "logs"
STATUS_PATH = PROJECT_ROOT / "orchestrator" / "status.json"
FAILURE_LIMIT = 3
CYCLE_INTERVAL_MINUTES = 5
CYCLE_DURATION_GUARD_SECONDS = 240
FAILURE_STAGES = (
    "fetcher",
    "indicator_engine",
    "signal_engine",
    "trade_execution",
    "trade_monitoring",
)
T = TypeVar("T")


class ISTFormatter(logging.Formatter):
    converter = staticmethod(lambda timestamp: datetime.fromtimestamp(timestamp, IST).timetuple())


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("orchestrator")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = ISTFormatter("[%(asctime)s IST] %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "orchestrator.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


class Orchestrator:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.logger = configure_logging()
        self.scheduler: BlockingScheduler | None = None
        self.cycle_number = 0
        self.session_opened = 0
        self.session_closed = 0
        self.today = datetime.now(IST).date()
        self.today_opened = 0
        self.today_closed = 0
        self.failures = {stage: 0 for stage in FAILURE_STAGES}
        self.disabled: set[str] = set()
        self.cycle_lock = threading.Lock()
        self.skip_next_cycle = False
        self.shutdown_requested = False

    def _roll_day(self) -> None:
        current = datetime.now(IST).date()
        if current != self.today:
            self.today = current
            self.today_opened = 0
            self.today_closed = 0

    def _is_market_open(self, now: datetime) -> bool:
        return now.weekday() < 5 and clock_time(9, 15) <= now.time() <= clock_time(15, 25)

    def _failure(self, stage: str) -> None:
        self.failures[stage] += 1
        if self.failures[stage] >= FAILURE_LIMIT:
            self.disabled.add(stage)
            self.logger.critical(
                "%s failed %d cycles in a row; disabled for this session",
                stage,
                self.failures[stage],
            )

    def _success(self, stage: str) -> None:
        self.failures[stage] = 0

    def _stage(self, stage: str, function: Callable[[], T], default: T) -> tuple[T, bool]:
        started = time.perf_counter()
        if stage in self.disabled:
            self.logger.warning("Stage %s skipped (disabled after repeated failures)", stage)
            self.logger.info("Stage %s duration: 0 ms", stage)
            return default, False
        try:
            result = function()
            self._success(stage)
            return result, True
        except Exception:
            self.logger.exception("Stage %s failed", stage)
            self._failure(stage)
            return default, False
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            self.logger.info("Stage %s duration: %.0f ms", stage, elapsed)

    def _latest_price(self, symbol: str) -> float | None:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT close FROM candles WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return float(row[0]) if row else None

    def _balance(self) -> float:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute("SELECT balance FROM paper_account ORDER BY id LIMIT 1").fetchone()
            return float(row[0]) if row else 0.0
        except sqlite3.Error:
            self.logger.exception("Could not read current balance")
            return 0.0

    def _scan_counts(self) -> tuple[int, int]:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*), SUM(
                        CASE WHEN direction != 'HOLD'
                              AND confidence_score >= COALESCE((
                                  SELECT min_score_to_trade FROM strategy_rules
                                  WHERE is_active = 1 ORDER BY version DESC LIMIT 1
                              ), 0)
                             THEN 1 ELSE 0 END
                    )
                    FROM signal_scores
                    WHERE scan_cycle = (SELECT MAX(scan_cycle) FROM signal_scores)
                    """
                ).fetchone()
            return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
        except sqlite3.Error:
            self.logger.exception("Could not read scan summary")
            return 0, 0

    def _next_run(self) -> str | None:
        if self.scheduler:
            # APScheduler 3.11 does not expose next_run_time until the scheduler
            # has started; the immediate startup cycle runs just before that.
            times = [
                next_run
                for job in self.scheduler.get_jobs()
                if (next_run := getattr(job, "next_run_time", None)) is not None
            ]
            if times:
                return min(times).astimezone(IST).replace(microsecond=0).isoformat()
        return None

    def _write_status(self, status: str, duration: float = 0.0) -> None:
        now = datetime.now(IST).replace(microsecond=0)
        payload = {
            "last_run": now.isoformat(),
            "next_run": self._next_run(),
            "cycle_number": self.cycle_number,
            "status": status,
            "last_cycle_duration_seconds": round(duration, 3),
            "cycle_interval_minutes": CYCLE_INTERVAL_MINUTES,
            "consecutive_failures": dict(self.failures),
            "trades_opened_today": self.today_opened,
            "trades_closed_today": self.today_closed,
            "current_balance": round(self._balance(), 2),
        }
        temporary = STATUS_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(STATUS_PATH)

    def _get_dynamic_max_trades(self, market_condition: str) -> dict[str, Any]:
        """Calculate dynamic max trades based on market condition.
        
        Base: 6 trades
        TRENDING: +2 (total 8)
        SIDEWAYS: 0 (total 6)
        VOLATILE: -2 (total 4)
        """
        base = 6
        adjustment = {
            "TRENDING": 2,
            "SIDEWAYS": 0,
            "VOLATILE": -2,
        }.get(market_condition, 0)
        
        max_trades = base + adjustment
        return {
            "max_trades": max_trades,
            "base": base,
            "adjustment": adjustment,
            "market_condition": market_condition,
        }

    def _should_make_room_for_signal(
        self, 
        new_signal: dict[str, Any], 
        open_trades: list[dict[str, Any]], 
        dynamic_max: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        """Even if at max capacity, check if this new signal is significantly better than any existing open trade.
        
        Parameters
        ----------
        new_signal : dict
            New signal with weighted_score
        open_trades : list
            Currently open trades with unrealized_pnl_pct and confidence_score
        dynamic_max : dict
            Dynamic max trades info with max_trades
            
        Returns
        -------
        tuple[bool, dict | None]
            (should_replace, weakest_trade_to_replace)
        """
        # If under capacity, no need to make room
        if len(open_trades) < dynamic_max['max_trades']:
            return False, None
        
        # Find the weakest open trade
        # Weakest = most negative unrealized PnL + lowest entry confidence score
        weakest_trade = min(
            open_trades,
            key=lambda t: (t['unrealized_pnl_pct'] * 0.7) + (t['confidence_score'] * 0.3)
        )
        
        # Only replace if new signal is significantly better
        score_advantage = new_signal['weighted_score'] - weakest_trade['confidence_score']
        trade_is_losing = weakest_trade['unrealized_pnl_pct'] < -0.3  # losing more than 0.3%
        new_signal_is_strong = new_signal['weighted_score'] >= 65
        
        if score_advantage >= 20 and trade_is_losing and new_signal_is_strong:
            return True, weakest_trade  # make room by closing weakest
        
        return False, None

    def _execute_picks(self, picks: list[dict[str, Any]], market_condition: str) -> int:
        opened = 0
        replaced = 0
        had_error = False
        if "trade_execution" in self.disabled:
            self.logger.warning("Stage trade_execution skipped (disabled after repeated failures)")
            return 0

        # Get dynamic max trades
        dynamic_max = self._get_dynamic_max_trades(market_condition)
        self.logger.info(
            "Dynamic max trades: %d (base=%d, %s=%+d)",
            dynamic_max['max_trades'],
            dynamic_max['base'],
            market_condition,
            dynamic_max['adjustment']
        )
        
        # Get current open trades with unrealized PnL
        try:
            open_trades = get_open_trades_with_unrealized_pnl() if not self.dry_run else []
        except Exception:
            self.logger.exception("Failed to get open trades; assuming empty")
            open_trades = []

        for pick in picks:
            symbol = str(pick.get("symbol", "UNKNOWN"))
            try:
                # Check if we should make room for this signal
                should_replace, weakest_trade = self._should_make_room_for_signal(
                    pick, open_trades, dynamic_max
                )
                
                if should_replace and weakest_trade:
                    self.logger.info(
                        "Trade replacement opportunity: %s (score: %.1f, PnL: %.2f%%) → %s (score: %.1f)",
                        weakest_trade['symbol'],
                        weakest_trade['confidence_score'],
                        weakest_trade['unrealized_pnl_pct'],
                        symbol,
                        pick['weighted_score']
                    )
                    
                    if not self.dry_run:
                        # Close the weakest trade
                        close_result = close_trade(weakest_trade['id'], 'REPLACED')
                        if close_result.get('success'):
                            replaced += 1
                            self.logger.info(
                                "Replaced %s (score: %.1f, PnL: %.2f%%) with %s (score: %.1f)",
                                weakest_trade['symbol'],
                                weakest_trade['confidence_score'],
                                weakest_trade['unrealized_pnl_pct'],
                                symbol,
                                pick['weighted_score']
                            )
                            # Remove from open_trades list
                            open_trades = [t for t in open_trades if t['id'] != weakest_trade['id']]
                        else:
                            self.logger.warning(
                                "Failed to close weakest trade %s: %s",
                                weakest_trade['symbol'],
                                close_result.get('reason')
                            )
                            continue
                    else:
                        self.logger.info(
                            "DRY RUN | Would replace %s (score: %.1f, PnL: %.2f%%) with %s (score: %.1f)",
                            weakest_trade['symbol'],
                            weakest_trade['confidence_score'],
                            weakest_trade['unrealized_pnl_pct'],
                            symbol,
                            pick['weighted_score']
                        )
                
                # Check if we're still at capacity (after potential replacement)
                if len(open_trades) >= dynamic_max['max_trades']:
                    self.logger.info(
                        "%s skipped: at max capacity (%d/%d)",
                        symbol,
                        len(open_trades),
                        dynamic_max['max_trades']
                    )
                    continue
                
                validation = validate_signal(pick)
                if not validation.get("approved"):
                    self.logger.info("%s rejected: %s", symbol, validation.get("reason", "unknown reason"))
                    continue
                entry_price = self._latest_price(symbol)
                if entry_price is None:
                    raise RuntimeError(f"No latest candle price for {symbol}")
                position = calculate_position(symbol, str(pick["direction"]), entry_price)
                if int(position.get("quantity", 0)) <= 0 or float(position.get("capital_required", 0)) <= 0:
                    self.logger.warning("%s has an invalid position; skipping", symbol)
                    continue
                self.logger.info(
                    (
                        "POSITION SIZE | %s %s | entry=%.2f stop=%.2f target=%.2f "
                        "risk/share=%.2f max_loss=%.2f qty_from_risk=%d "
                        "max_capital=%.2f max_qty_cap=%d final_qty=%d capital=%.2f"
                    ),
                    pick["direction"],
                    symbol,
                    position["entry_price"],
                    position["stop_loss_price"],
                    position["target_price"],
                    position["risk_per_share"],
                    position["max_loss_amount"],
                    position["quantity_from_risk"],
                    position["max_capital_per_trade"],
                    position["max_quantity_from_capital"],
                    position["quantity"],
                    position["capital_required"],
                )
                if self.dry_run:
                    self.logger.info(
                        "DRY RUN | Would open %s %s x%d at %.2f",
                        pick["direction"], symbol, position["quantity"], position["entry_price"],
                    )
                    continue
                result = open_trade(pick)
                if result.get("success"):
                    opened += 1
                    # Add to open_trades list for next iteration
                    open_trades.append({
                        "id": result.get("trade_id"),
                        "symbol": symbol,
                        "confidence_score": pick.get("weighted_score", 0),
                        "unrealized_pnl_pct": 0.0,
                    })
                    self.logger.info("Opened %s trade_id=%s", symbol, result.get("trade_id"))
                else:
                    self.logger.warning("Could not open %s: %s", symbol, result.get("reason"))
            except Exception:
                had_error = True
                self.logger.exception("Trade execution failed for %s; continuing with remaining picks", symbol)

        if had_error:
            self._failure("trade_execution")
        else:
            self._success("trade_execution")
        
        if replaced > 0:
            self.logger.info("Trade replacements: %d", replaced)
        
        return opened

    def run_cycle(self, ignore_market_hours: bool = False) -> None:
        if self.skip_next_cycle and not ignore_market_hours:
            self.skip_next_cycle = False
            self.logger.warning("Previous cycle exceeded 4 minutes; skipping this scheduled cycle")
            return
        if not self.cycle_lock.acquire(blocking=False):
            self.logger.warning("Previous cycle still running; skipping this trigger")
            return
        cycle_started = time.perf_counter()
        self._roll_day()
        self.cycle_number += 1
        now = datetime.now(IST)
        self.logger.info("CYCLE #%d started at %s", self.cycle_number, now.isoformat())
        scanned = signals = opened = closed_count = 0
        market_condition = "SIDEWAYS"  # default
        try:
            market_started = time.perf_counter()
            market_open = self._is_market_open(now)
            self.logger.info("Stage market_check duration: %.0f ms", (time.perf_counter() - market_started) * 1000)
            if not ignore_market_hours and not market_open:
                self.logger.info("Market closed, skipping")
                return

            self._stage("fetcher", run_fetcher, None)
            self._stage("indicator_engine", run_indicator_engine, [])
            picks, _ = self._stage("signal_engine", run_scan, [])
            scanned, signals = self._scan_counts()
            
            # Extract market condition from picks if available
            if picks and len(picks) > 0:
                market_condition = picks[0].get("market_condition", "SIDEWAYS")

            execution_started = time.perf_counter()
            opened = self._execute_picks(picks, market_condition)
            self.logger.info("Stage trade_execution duration: %.0f ms", (time.perf_counter() - execution_started) * 1000)

            if self.dry_run:
                self.logger.info("DRY RUN | Stage trade_monitoring skipped; no trades will be closed")
                self.logger.info("Stage trade_monitoring duration: 0 ms")
                closed: list[dict[str, Any]] = []
            else:
                closed, _ = self._stage("trade_monitoring", monitor_open_trades, [])
            closed_count = len(closed)

            stats_started = time.perf_counter()
            if closed_count:
                try:
                    update_stats()
                except Exception:
                    self.logger.exception("Stage performance_stats failed")
            self.logger.info("Stage performance_stats duration: %.0f ms", (time.perf_counter() - stats_started) * 1000)
            self.session_opened += opened
            self.session_closed += closed_count
            self.today_opened += opened
            self.today_closed += closed_count
        finally:
            duration = time.perf_counter() - cycle_started
            balance = self._balance()
            self.logger.info(
                "CYCLE #%d | Duration: %.1fs | Scanned: %d | Signals: %d | Opened: %d | Closed: %d | Balance: ₹%s",
                self.cycle_number, duration, scanned, signals, opened, closed_count, f"{balance:,.0f}",
            )
            self._write_status("running", duration)
            if duration > CYCLE_DURATION_GUARD_SECONDS:
                self.skip_next_cycle = True
                self.logger.warning(
                    "Cycle duration %.1fs exceeded %.0fs guard; next scheduled cycle will be skipped",
                    duration,
                    CYCLE_DURATION_GUARD_SECONDS,
                )
            self.cycle_lock.release()

    def run_eod_cycle(self) -> None:
        if self.dry_run:
            self.logger.info("DRY RUN | EOD monitor skipped")
            return
        if not self.cycle_lock.acquire(blocking=False):
            self.logger.warning("Normal cycle still running; EOD cycle will wait for it")
            self.cycle_lock.acquire()
        started = time.perf_counter()
        self._roll_day()
        self.cycle_number += 1
        closed_count = 0
        try:
            self.logger.info("EOD CYCLE #%d started", self.cycle_number)
            closed, _ = self._stage("trade_monitoring", monitor_open_trades, [])
            closed_count = len(closed)
            if closed_count:
                try:
                    update_stats()
                except Exception:
                    self.logger.exception("EOD performance stats update failed")
            self.session_closed += closed_count
            self.today_closed += closed_count
        finally:
            duration = time.perf_counter() - started
            self.logger.info("EOD CYCLE #%d | Duration: %.1fs | Closed: %d", self.cycle_number, duration, closed_count)
            self._write_status("running", duration)
            self.cycle_lock.release()

    def request_shutdown(self, *_: Any) -> None:
        if self.shutdown_requested:
            return
        self.shutdown_requested = True
        self.logger.info("Shutdown requested; any active cycle will finish first")
        if self.scheduler and self.scheduler.running:
            threading.Thread(target=self.scheduler.shutdown, kwargs={"wait": True}, daemon=True).start()

    def shutdown(self) -> None:
        with self.cycle_lock:
            self._write_status("stopped")
        self.logger.info(
            "SHUTDOWN SUMMARY | Cycles: %d | Opened: %d | Closed: %d | Final balance: ₹%s",
            self.cycle_number, self.session_opened, self.session_closed, f"{self._balance():,.0f}",
        )

    def start(self) -> None:
        self.scheduler = BlockingScheduler(timezone=IST)
        self.scheduler.add_job(
            self.run_cycle,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=IST),
            id="market_cycles", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self.run_eod_cycle,
            CronTrigger(day_of_week="mon-fri", hour="15", minute="30", timezone=IST),
            id="eod_cycle", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            run_learning_cycle,
            CronTrigger(day_of_week="sat", hour="10", minute="0", timezone=IST),
            id="weekly_learning", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            update_universe,
            CronTrigger(day_of_week="sun", hour="9", minute="0", timezone=IST),
            id="universe_update", max_instances=1, coalesce=True,
        )
        # Startup is intentionally immediate; the regular market-hours guard is bypassed.
        self.run_cycle(ignore_market_hours=True)
        self.logger.info("Scheduler started%s", " in DRY RUN mode" if self.dry_run else "")
        try:
            self.scheduler.start()
        finally:
            self.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-once", action="store_true", help="Run one immediate cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Never open or close paper trades")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    orchestrator = Orchestrator(dry_run=args.dry_run)
    signal.signal(signal.SIGINT, orchestrator.request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, orchestrator.request_shutdown)
    if args.run_once:
        try:
            orchestrator.run_cycle(ignore_market_hours=True)
        finally:
            orchestrator.shutdown()
        return
    orchestrator.start()


if __name__ == "__main__":
    main()
