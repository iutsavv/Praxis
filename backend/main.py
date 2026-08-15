import asyncio
import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "database" / "trading.db"
ORCHESTRATOR_STATUS_PATH = PROJECT_ROOT / "orchestrator" / "status.json"
IST = ZoneInfo("Asia/Kolkata")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

app = FastAPI(title="AI Trading Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.options("/{path:path}")
async def preflight_handler(path: str) -> JSONResponse:
    return JSONResponse({"ok": True})


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def has_columns(conn: sqlite3.Connection, table_name: str, required: set[str]) -> bool:
    return required.issubset(table_columns(conn, table_name))


def sentiment_label(score: float) -> str:
    if score >= 0.6:
        return "VERY_POSITIVE"
    if score >= 0.2:
        return "POSITIVE"
    if score <= -0.6:
        return "VERY_NEGATIVE"
    if score <= -0.2:
        return "NEGATIVE"
    return "NEUTRAL"


def parse_json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def ensure_signal_scores_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(signal_scores)")}
    if "timeframe_confluence" not in columns:
        conn.execute("ALTER TABLE signal_scores ADD COLUMN timeframe_confluence TEXT")
        conn.commit()
    if "announcement_boost" not in columns:
        conn.execute("ALTER TABLE signal_scores ADD COLUMN announcement_boost REAL DEFAULT 0")
        conn.commit()


def ist_now() -> datetime:
    return datetime.now(IST)


def iso_now() -> str:
    return ist_now().replace(microsecond=0).isoformat()


def pipeline_error(stage: str, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "stage": stage,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=5),
        },
    )


def is_market_open() -> bool:
    now = ist_now()
    return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 25)


def get_orchestrator_health() -> dict[str, Any]:
    fallback = {
        "orchestrator_alive": False,
        "last_run": None,
        "next_run": None,
        "cycle_interval_minutes": 5,
        "cycle_number": 0,
        "consecutive_failures": {},
    }
    try:
        payload = json.loads(ORCHESTRATOR_STATUS_PATH.read_text(encoding="utf-8"))
        last_run_text = payload.get("last_run")
        last_run = datetime.fromisoformat(last_run_text) if last_run_text else None
        if last_run and last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=IST)
        alive = bool(
            payload.get("status") == "running"
            and last_run
            and timedelta(0) <= ist_now() - last_run.astimezone(IST) <= timedelta(minutes=20)
        )
        return {
            "orchestrator_alive": alive,
            "last_run": last_run_text,
            "next_run": payload.get("next_run"),
            "cycle_interval_minutes": payload.get("cycle_interval_minutes", 5),
            "cycle_number": payload.get("cycle_number", 0),
            "consecutive_failures": payload.get("consecutive_failures", {}),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return fallback


def latest_price(conn: sqlite3.Connection, symbol: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM candles
        WHERE symbol = ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return float(row["close"]) if row else None


def get_latest_signal(conn: sqlite3.Connection, symbol: str) -> dict[str, Any] | None:
    ensure_signal_scores_schema(conn)
    row = conn.execute(
        """
        SELECT id, symbol, scan_cycle, confidence_score, direction,
               market_condition, contrib_rsi, contrib_macd, contrib_volume,
               contrib_vwap, explanation, notes, timeframe_confluence,
               announcement_boost, created_at
        FROM signal_scores
        WHERE UPPER(symbol) = UPPER(?)
        ORDER BY scan_cycle DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return None

    signal = dict(row)
    signal["weighted_score"] = signal["confidence_score"]
    try:
        signal["timeframe_confluence"] = json.loads(signal.get("timeframe_confluence") or "{}")
    except json.JSONDecodeError:
        signal["timeframe_confluence"] = {}
    return signal


def latest_table_timestamp(
    conn: sqlite3.Connection,
    table_name: str,
    columns: list[str],
) -> str | None:
    table_columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")
    }
    available = [column for column in columns if column in table_columns]
    if not available:
        return None

    union_parts = [
        f"SELECT MAX({column}) AS value FROM {table_name}"
        for column in available
    ]
    row = conn.execute(
        f"SELECT MAX(value) AS latest FROM ({' UNION ALL '.join(union_parts)})"
    ).fetchone()
    return row["latest"] if row and row["latest"] else None


def get_signal_scores(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_signal_scores_schema(conn)
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, MAX(scan_cycle) AS scan_cycle
            FROM signal_scores
            GROUP BY symbol
        )
        SELECT s.id, s.symbol, s.scan_cycle, s.confidence_score, s.direction,
               s.market_condition, s.contrib_rsi, s.contrib_macd,
               s.contrib_volume, s.contrib_vwap, s.explanation,
               s.timeframe_confluence, s.announcement_boost, s.created_at
        FROM signal_scores s
        JOIN latest l ON l.symbol = s.symbol AND l.scan_cycle = s.scan_cycle
        ORDER BY s.confidence_score DESC, s.symbol ASC
        """
    ).fetchall()

    if rows:
        signals = rows_to_dicts(rows)
        for signal in signals:
            try:
                signal["timeframe_confluence"] = json.loads(
                    signal.get("timeframe_confluence") or "{}"
                )
            except json.JSONDecodeError:
                signal["timeframe_confluence"] = {}
        return signals

    stocks = conn.execute(
        "SELECT symbol FROM stocks WHERE is_active = 1 ORDER BY symbol"
    ).fetchall()
    return [
        {
            "id": None,
            "symbol": row["symbol"],
            "scan_cycle": None,
            "confidence_score": 0,
            "direction": "HOLD",
            "market_condition": "SIDEWAYS",
            "contrib_rsi": 0,
            "contrib_macd": 0,
            "contrib_volume": 0,
            "contrib_vwap": 0,
            "explanation": "No signal scan has been written yet.",
            "created_at": None,
        }
        for row in stocks
    ]


def get_open_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE status = 'OPEN'
        ORDER BY entry_time DESC
        """
    ).fetchall()
    trades = rows_to_dicts(rows)
    for trade in trades:
        current_price = latest_price(conn, trade["symbol"]) or float(trade["entry_price"])
        direction_multiplier = 1 if trade["direction"] == "BUY" else -1
        trade["current_price"] = current_price
        trade["unrealized_pnl"] = round(
            (current_price - float(trade["entry_price"]))
            * int(trade["quantity"])
            * direction_multiplier,
            2,
        )
    return trades


def get_trade_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE status = 'CLOSED'
        ORDER BY COALESCE(exit_time, updated_at, created_at) DESC
        """
    ).fetchall()
    return rows_to_dicts(rows)


def get_account_row(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM paper_account ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_active_strategy(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM strategy_rules
        WHERE is_active = 1
        ORDER BY version DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


class ExecuteTradeRequest(BaseModel):
    symbol: str


class BacktestRequest(BaseModel):
    date_from: str
    date_to: str
    symbol: str | None = None
    strategy_version: int | None = None


def run_indicators_stage() -> dict[str, Any]:
    from analysis import indicator_engine

    conn = indicator_engine._get_connection()
    results = []
    errors: list[dict[str, str]] = []
    try:
        symbols = indicator_engine.get_active_symbols(conn)
        for symbol in symbols:
            try:
                results.append(indicator_engine.process_symbol(conn, symbol))
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
        if results:
            indicator_engine.print_run_summary(results)
    finally:
        conn.close()

    return {
        "status": "success",
        "symbols_processed": len(results),
        "indicators_computed": sum(result.indicators_inserted for result in results),
        "errors": errors,
    }


def run_scan_stage() -> dict[str, Any]:
    from analysis import signal_engine

    cfg = signal_engine.load_config()
    conn = signal_engine._get_connection()
    results = []
    errors: list[dict[str, str]] = []
    try:
        symbols = signal_engine.get_active_symbols(conn)
        weights = signal_engine.get_strategy_weights(conn)
        market_condition = signal_engine.detect_market_condition(conn, cfg)
        scan_cycle = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for symbol in symbols:
            try:
                result = signal_engine.score_stock(
                    conn,
                    symbol,
                    weights,
                    market_condition,
                    cfg,
                )
                if result is None:
                    errors.append({"symbol": symbol, "error": "no indicator data"})
                    continue
                results.append(result)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})

        if results:
            signal_engine.write_signal_scores(conn, results, scan_cycle)

        top_picks = signal_engine.get_top_picks(results, weights.min_score_to_trade)
        above_threshold = [
            result
            for result in results
            if result.direction != "HOLD"
            and result.weighted_score >= weights.min_score_to_trade
        ]
    finally:
        conn.close()

    return {
        "status": "success",
        "stocks_scanned": len(results),
        "signals_above_threshold": len(above_threshold),
        "top_picks": top_picks,
        "errors": errors,
    }


def run_two_stage_scan_stage() -> dict[str, Any]:
    from analysis.stage1_scanner import run_stage1_scan
    from analysis.stage2_scanner import run_stage2_scan
    from analysis.indicator_engine import process_symbol_for_intervals
    from data.fetcher import (
        _get_connection as get_fetcher_connection,
        fetch_candles,
        get_latest_timestamp_for_interval,
        store_candles,
    )

    flagged, scan_time = run_stage1_scan()

    symbols_needing_data: list[str] = []
    with get_db() as conn:
        for symbol in flagged:
            row = conn.execute(
                """
                SELECT COUNT(*) AS candle_count
                FROM candles
                WHERE symbol = ? AND interval = '15m'
                  AND timestamp >= datetime('now', '-30 minutes')
                """,
                (symbol,),
            ).fetchone()
            if not row or int(row["candle_count"] or 0) == 0:
                symbols_needing_data.append(symbol)

    data_errors: list[dict[str, str]] = []
    if symbols_needing_data:
        fetcher_conn = get_fetcher_connection()
        indicator_conn = get_db()
        try:
            from data.fetcher import fetch_candles_bulk
            for interval in ("5m", "15m", "1h", "1d"):
                bulk_data = fetch_candles_bulk(symbols_needing_data, interval)
                for symbol in symbols_needing_data:
                    try:
                        df = bulk_data.get(symbol, pd.DataFrame())
                        if not df.empty:
                            store_candles(fetcher_conn, symbol, df, interval)
                    except Exception as exc:
                        data_errors.append({"symbol": symbol, "error": f"{interval}: {exc}"})
            for symbol in symbols_needing_data:
                try:
                    process_symbol_for_intervals(indicator_conn, symbol)
                except Exception as exc:
                    data_errors.append({"symbol": symbol, "error": f"indicator: {exc}"})
        finally:
            fetcher_conn.close()
            indicator_conn.close()

    top_picks = run_stage2_scan(flagged, scan_time)
    return {
        "status": "success",
        "scan_time": scan_time,
        "stage1_flagged": len(flagged),
        "data_refreshed": len(symbols_needing_data) - len(data_errors),
        "stage2_candidates": len(top_picks),
        "top_picks": top_picks,
        "errors": data_errors,
    }


def execute_trade_stage(symbol: str) -> dict[str, Any]:
    from agents import paper_trader, risk_manager, trade_validator

    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol is required")

    with get_db() as conn:
        signal = get_latest_signal(conn, symbol)
        if signal is None:
            return {
                "validation": {
                    "approved": False,
                    "reason": f"No signal found for {symbol}",
                },
                "position": None,
                "trade": None,
            }
        entry_price = latest_price(conn, symbol)

    validation = trade_validator.validate_signal(signal)
    if not validation.get("approved"):
        return {
            "validation": validation,
            "position": None,
            "trade": None,
        }

    if entry_price is None:
        return {
            "validation": {
                "approved": False,
                "reason": f"No latest candle price found for {symbol}",
            },
            "position": None,
            "trade": None,
        }

    position = risk_manager.calculate_position(
        symbol,
        str(signal["direction"]),
        float(entry_price),
    )
    if int(position.get("quantity", 0)) <= 0 or float(position.get("capital_required", 0)) <= 0:
        return {
            "validation": validation,
            "position": position,
            "trade": {
                "success": False,
                "reason": "Invalid position size",
            },
        }

    trade = paper_trader.open_trade(signal)
    return {
        "validation": validation,
        "position": position,
        "trade": trade,
    }


def monitor_trades_stage() -> dict[str, Any]:
    from agents import paper_trader

    details = paper_trader.monitor_open_trades()
    return {
        "status": "success",
        "trades_closed": len(details),
        "details": details,
    }


@app.get("/api/ping")
async def ping() -> dict[str, Any]:
    database_status = "connected"
    total_candles = 0
    total_trades = 0
    try:
        with get_db() as conn:
            total_candles = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            total_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    except sqlite3.Error:
        database_status = "disconnected"

    return {
        "status": "ok" if database_status == "connected" else "error",
        "timestamp": iso_now(),
        "database": database_status,
        "total_candles": total_candles,
        "total_trades": total_trades,
        "market_open": is_market_open(),
        **get_orchestrator_health(),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return get_orchestrator_health()


@app.get("/api/account")
@app.get("/api/account/summary")
async def account_summary():
    with get_db() as conn:
        account = get_account_row(conn)
    if account is None:
        return JSONResponse(
            status_code=500,
            content={
                "error": "paper_account not initialized. Run database init first."
            },
        )
    return account


@app.post("/api/pipeline/run-indicators")
async def run_indicators():
    try:
        return run_indicators_stage()
    except Exception as exc:
        return pipeline_error("indicators", exc)


@app.post("/api/pipeline/run-scan")
async def run_scan():
    try:
        return run_scan_stage()
    except Exception as exc:
        return pipeline_error("scan", exc)


@app.post("/api/pipeline/run-two-stage-scan")
async def run_two_stage_scan():
    try:
        return await asyncio.to_thread(run_two_stage_scan_stage)
    except Exception as exc:
        return pipeline_error("two_stage_scan", exc)


@app.post("/api/pipeline/execute-trade")
async def execute_trade(request: ExecuteTradeRequest):
    try:
        return execute_trade_stage(request.symbol)
    except Exception as exc:
        return pipeline_error("trade_execution", exc)


@app.post("/api/pipeline/monitor-trades")
async def monitor_trades():
    try:
        return monitor_trades_stage()
    except Exception as exc:
        return pipeline_error("monitoring", exc)


@app.post("/api/pipeline/run-full-cycle")
async def run_full_cycle():
    summary: dict[str, Any] = {"status": "success", "started_at": iso_now()}
    try:
        summary["indicators"] = run_indicators_stage()
    except Exception as exc:
        return pipeline_error("indicators", exc)

    try:
        scan_result = run_scan_stage()
        summary["scan"] = scan_result
    except Exception as exc:
        return pipeline_error("scan", exc)

    executions = []
    for pick in scan_result.get("top_picks", []):
        symbol = str(pick.get("symbol", ""))
        try:
            executions.append(
                {
                    "symbol": symbol,
                    "result": execute_trade_stage(symbol),
                }
            )
        except Exception as exc:
            executions.append(
                {
                    "symbol": symbol,
                    "result": {
                        "status": "error",
                        "stage": "trade_execution",
                        "message": str(exc),
                        "traceback": traceback.format_exc(limit=5),
                    },
                }
            )
    summary["trade_executions"] = executions

    try:
        summary["monitoring"] = monitor_trades_stage()
    except Exception as exc:
        return pipeline_error("monitoring", exc)

    summary["finished_at"] = iso_now()
    return summary


@app.get("/api/pipeline/status")
async def pipeline_status():
    try:
        with get_db() as conn:
            return {
                "status": "success",
                "indicators": latest_table_timestamp(
                    conn,
                    "indicators",
                    ["timestamp", "created_at"],
                ),
                "scan": latest_table_timestamp(
                    conn,
                    "signal_scores",
                    ["created_at", "scan_cycle"],
                ),
                "trade_execution": latest_table_timestamp(
                    conn,
                    "paper_trades",
                    ["created_at", "entry_time", "updated_at"],
                ),
                "monitoring": latest_table_timestamp(
                    conn,
                    "trade_logs",
                    ["timestamp"],
                ),
            }
    except Exception as exc:
        return pipeline_error("status", exc)


@app.get("/api/signals")
async def signals() -> list[dict[str, Any]]:
    with get_db() as conn:
        return get_signal_scores(conn)


@app.get("/api/universe/summary")
async def universe_summary() -> dict[str, Any]:
    with get_db() as conn:
        if not table_exists(conn, "stock_universe"):
            return {
                "total_stocks": 0,
                "fo_stocks_count": 0,
                "stage2_eligible_count": 0,
                "sector_distribution": {},
                "last_updated": None,
            }
        cols = table_columns(conn, "stock_universe")
        total = conn.execute("SELECT COUNT(*) FROM stock_universe WHERE COALESCE(is_active, 1) = 1").fetchone()[0]
        fo_count = conn.execute(
            "SELECT COUNT(*) FROM stock_universe WHERE COALESCE(is_active, 1) = 1 AND COALESCE(is_fo_stock, 0) = 1"
        ).fetchone()[0] if "is_fo_stock" in cols else 0
        stage2_count = conn.execute(
            "SELECT COUNT(*) FROM stock_universe WHERE COALESCE(is_active, 1) = 1 AND COALESCE(in_stage2_scan, 0) = 1"
        ).fetchone()[0] if "in_stage2_scan" in cols else 0
        sector_distribution = {}
        if "sector" in cols:
            sector_rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(sector, ''), 'Unknown') AS sector, COUNT(*) AS count
                FROM stock_universe
                WHERE COALESCE(is_active, 1) = 1
                GROUP BY COALESCE(NULLIF(sector, ''), 'Unknown')
                ORDER BY count DESC, sector ASC
                """
            ).fetchall()
            sector_distribution = {row["sector"]: row["count"] for row in sector_rows}
        last_updated = latest_table_timestamp(conn, "stock_universe", ["last_updated", "added_at"])
        return {
            "total_stocks": total,
            "fo_stocks_count": fo_count,
            "stage2_eligible_count": stage2_count,
            "sector_distribution": sector_distribution,
            "last_updated": last_updated,
        }


@app.get("/api/universe/stocks")
async def universe_stocks(
    sector: str | None = None,
    is_fo: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with get_db() as conn:
        if not table_exists(conn, "stock_universe"):
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        where = ["COALESCE(is_active, 1) = 1"]
        params: list[Any] = []
        cols = table_columns(conn, "stock_universe")
        if sector and "sector" in cols:
            where.append("sector = ?")
            params.append(sector)
        if is_fo is not None and "is_fo_stock" in cols:
            where.append("COALESCE(is_fo_stock, 0) = ?")
            params.append(1 if int(is_fo) else 0)
        where_sql = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM stock_universe WHERE {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT symbol, name, sector, market_cap, avg_daily_volume, is_fo_stock, in_stage2_scan
            FROM stock_universe
            WHERE {where_sql}
            ORDER BY symbol
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return {"items": rows_to_dicts(rows), "total": total, "limit": limit, "offset": offset}


@app.get("/api/scan/latest")
async def scan_latest() -> dict[str, Any]:
    with get_db() as conn:
        if not table_exists(conn, "scan_results"):
            return {
                "scan_time": None,
                "stage1_total_scanned": 0,
                "stage1_flagged": 0,
                "stage2_analyzed": 0,
                "stage2_candidates": 0,
                "top_picks": [],
                "duration_seconds": None,
            }
        latest = conn.execute("SELECT MAX(scan_time) AS scan_time FROM scan_results").fetchone()["scan_time"]
        if not latest:
            return {
                "scan_time": None,
                "stage1_total_scanned": 0,
                "stage1_flagged": 0,
                "stage2_analyzed": 0,
                "stage2_candidates": 0,
                "top_picks": [],
                "duration_seconds": None,
            }
        total_scanned = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_universe WHERE COALESCE(is_active, 1) = 1").fetchone()[0] if table_exists(conn, "stock_universe") else 0
        stage1_flagged = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM scan_results WHERE scan_time = ? AND stage1_flagged = 1",
            (latest,),
        ).fetchone()[0]
        stage2_analyzed = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM scan_results WHERE scan_time = ? AND stage = 2",
            (latest,),
        ).fetchone()[0]
        stage2_candidates = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM scan_results WHERE scan_time = ? AND final_selected = 1",
            (latest,),
        ).fetchone()[0]
        top_rows = conn.execute(
            """
            SELECT symbol, stage2_score AS score, stage2_direction AS direction,
                   stage2_pattern AS pattern, stage2_setup AS confluence_level
            FROM scan_results
            WHERE scan_time = ? AND stage = 2
            ORDER BY final_selected DESC, COALESCE(stage2_score, 0) DESC
            LIMIT 10
            """,
            (latest,),
        ).fetchall()
        return {
            "scan_time": latest,
            "stage1_total_scanned": total_scanned,
            "stage1_flagged": stage1_flagged,
            "stage2_analyzed": stage2_analyzed,
            "stage2_candidates": stage2_candidates,
            "top_picks": rows_to_dicts(top_rows),
            "duration_seconds": None,
        }


@app.get("/api/scan/stage1/flagged")
async def scan_stage1_flagged() -> list[dict[str, Any]]:
    with get_db() as conn:
        if not table_exists(conn, "scan_results"):
            return []
        latest = conn.execute("SELECT MAX(scan_time) AS scan_time FROM scan_results WHERE stage1_flagged = 1").fetchone()["scan_time"]
        if not latest:
            return []
        rows = conn.execute(
            """
            SELECT symbol, stage1_price_change, stage1_volume_ratio, stage1_reason
            FROM scan_results
            WHERE scan_time = ? AND stage1_flagged = 1
            ORDER BY ABS(COALESCE(stage1_price_change, 0)) DESC, COALESCE(stage1_volume_ratio, 0) DESC
            """,
            (latest,),
        ).fetchall()
        return rows_to_dicts(rows)


@app.get("/api/scan/stage2/results")
async def scan_stage2_results() -> list[dict[str, Any]]:
    with get_db() as conn:
        if not table_exists(conn, "scan_results"):
            return []
        latest = conn.execute("SELECT MAX(scan_time) AS scan_time FROM scan_results WHERE stage = 2").fetchone()["scan_time"]
        if not latest:
            return []
        rows = conn.execute(
            """
            SELECT symbol, stage2_score AS score, stage2_direction AS direction,
                   stage2_pattern AS pattern, stage2_setup AS confluence_level,
                   final_selected
            FROM scan_results
            WHERE scan_time = ? AND stage = 2
            ORDER BY COALESCE(stage2_score, 0) DESC
            """,
            (latest,),
        ).fetchall()
        return rows_to_dicts(rows)


@app.get("/api/news/latest")
async def news_latest(
    symbol: str | None = None,
    sentiment: str | None = None,
    urgency: str | None = None,
    since: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    with get_db() as conn:
        if not table_exists(conn, "news"):
            return []
        where = ["1=1"]
        params: list[Any] = []
        if symbol:
            where.append("(UPPER(symbol) = UPPER(?) OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?)")
            params.extend([symbol, f"%{symbol.upper()}%"])
        if sentiment == "positive":
            where.append("COALESCE(sentiment_score, 0) > 0.2")
        elif sentiment == "negative":
            where.append("COALESCE(sentiment_score, 0) < -0.2")
        elif sentiment == "neutral":
            where.append("COALESCE(sentiment_score, 0) BETWEEN -0.2 AND 0.2")
        if urgency:
            where.append("UPPER(COALESCE(urgency, 'LOW')) = UPPER(?)")
            params.append(urgency)
        if since == "30min":
            where.append("COALESCE(published_at, fetched_at) >= datetime('now', '-30 minutes')")
        rows = conn.execute(
            f"""
            SELECT id, symbol, headline, source, url, published_at,
                   sentiment_score, urgency, stocks_mentioned
            FROM news
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        items = rows_to_dicts(rows)
        for item in items:
            item["stocks_mentioned"] = parse_json_list(item.get("stocks_mentioned"))
        return items


@app.get("/api/news/announcements")
async def news_announcements(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    with get_db() as conn:
        if not table_exists(conn, "announcements"):
            return []
        rows = conn.execute(
            """
            SELECT id, symbol, exchange, announcement_type, headline, published_at,
                   sentiment_score, signal_boost
            FROM announcements
            ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows_to_dicts(rows)


@app.get("/api/news/stock/{symbol}")
async def news_for_stock(symbol: str) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    with get_db() as conn:
        if table_exists(conn, "news"):
            rows = conn.execute(
                """
                SELECT id, symbol, headline, source, url, published_at,
                       sentiment_score, urgency, stocks_mentioned
                FROM news
                WHERE UPPER(symbol) = UPPER(?) OR UPPER(COALESCE(stocks_mentioned, '')) LIKE ?
                ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                LIMIT 10
                """,
                (symbol, f"%{symbol.upper()}%"),
            ).fetchall()
            combined.extend(dict(row) | {"kind": "news"} for row in rows)
        if table_exists(conn, "announcements"):
            rows = conn.execute(
                """
                SELECT id, symbol, exchange, announcement_type, headline, published_at,
                       sentiment_score, signal_boost
                FROM announcements
                WHERE UPPER(symbol) = UPPER(?)
                ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                LIMIT 10
                """,
                (symbol,),
            ).fetchall()
            combined.extend(dict(row) | {"kind": "announcement"} for row in rows)
    return sorted(combined, key=lambda item: item.get("published_at") or "", reverse=True)[:20]


@app.get("/api/news/market-sentiment")
async def market_sentiment() -> dict[str, Any]:
    with get_db() as conn:
        empty = {
            "overall_score": 0.0,
            "label": "NEUTRAL",
            "article_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "top_positive_stock": None,
            "top_negative_stock": None,
            "sector_sentiment": {},
            "most_discussed": [],
            "recent_announcements_count": 0,
        }
        if not table_exists(conn, "news"):
            return empty
        if table_exists(conn, "stock_universe"):
            rows = conn.execute(
                """
                SELECT n.symbol, n.headline, n.sentiment_score, n.published_at,
                       COALESCE(su.sector, 'Unknown') AS sector
                FROM news n
                LEFT JOIN stock_universe su ON UPPER(su.symbol) = UPPER(n.symbol)
                WHERE COALESCE(n.published_at, n.fetched_at) >= datetime('now', '-4 hours')
                """,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT symbol, headline, sentiment_score, published_at, 'Unknown' AS sector
                FROM news
                WHERE COALESCE(published_at, fetched_at) >= datetime('now', '-4 hours')
                """,
            ).fetchall()
        if not rows:
            return empty
        scores = [float(row["sentiment_score"] or 0.0) for row in rows]
        overall = round(sum(scores) / len(scores), 4)
        sector_bucket: dict[str, list[float]] = {}
        counts: dict[str, int] = {}
        for row in rows:
            sector_bucket.setdefault(row["sector"] or "Unknown", []).append(float(row["sentiment_score"] or 0.0))
            if row["symbol"]:
                counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
        positives = [row for row in rows if float(row["sentiment_score"] or 0.0) > 0.2]
        negatives = [row for row in rows if float(row["sentiment_score"] or 0.0) < -0.2]
        top_pos = max(rows, key=lambda row: float(row["sentiment_score"] or 0.0))
        top_neg = min(rows, key=lambda row: float(row["sentiment_score"] or 0.0))
        recent_ann = 0
        if table_exists(conn, "announcements"):
            recent_ann = conn.execute(
                "SELECT COUNT(*) FROM announcements WHERE COALESCE(published_at, fetched_at) >= datetime('now', '-1 hour')"
            ).fetchone()[0]
        return {
            "overall_score": overall,
            "label": sentiment_label(overall),
            "article_count": len(rows),
            "positive_count": len(positives),
            "negative_count": len(negatives),
            "top_positive_stock": {"symbol": top_pos["symbol"], "headline": top_pos["headline"], "score": top_pos["sentiment_score"]},
            "top_negative_stock": {"symbol": top_neg["symbol"], "headline": top_neg["headline"], "score": top_neg["sentiment_score"]},
            "sector_sentiment": {
                sector: round(sum(values) / len(values), 4) for sector, values in sector_bucket.items()
            },
            "most_discussed": [
                {"symbol": symbol, "article_count": count}
                for symbol, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
            ],
            "recent_announcements_count": recent_ann,
        }


@app.get("/api/news/sentiment/{symbol}")
async def stock_sentiment(symbol: str) -> dict[str, Any]:
    try:
        from analysis.sentiment_engine import get_stock_news_sentiment

        summary = get_stock_news_sentiment(symbol)
    except Exception:
        summary = {
            "symbol": symbol.upper(),
            "overall_sentiment": 0.0,
            "article_count": 0,
            "announcement_count": 0,
            "highest_impact": "",
            "signal_modifier": 0.0,
            "urgency": "LOW",
        }
    with get_db() as conn:
        latest_announcement_type = None
        if table_exists(conn, "announcements") and has_columns(
            conn,
            "announcements",
            {"symbol", "announcement_type", "published_at"},
        ):
            row = conn.execute(
                """
                SELECT announcement_type
                FROM announcements
                WHERE UPPER(symbol) = UPPER(?)
                  AND date(COALESCE(published_at, fetched_at)) = date('now')
                ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            latest_announcement_type = row["announcement_type"] if row else None
    summary["latest_headline"] = summary.pop("highest_impact", "")
    summary["latest_announcement_type"] = latest_announcement_type
    return summary


@app.get("/api/signals/confluence/{symbol}")
async def signal_confluence(symbol: str) -> dict[str, Any]:
    timeframes = {}
    score = 0.0
    with get_db() as conn:
        if not table_exists(conn, "indicators"):
            return {"symbol": symbol.upper(), "timeframes": {}, "confluence_score": 0.0, "confluence_level": "LOW", "trade_direction": "SKIP"}
        for timeframe in ("5m", "15m", "1h", "1d"):
            row = conn.execute(
                """
                SELECT rsi, macd_histogram
                FROM indicators
                WHERE UPPER(symbol) = UPPER(?) AND interval = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (symbol, timeframe),
            ).fetchone()
            if not row:
                timeframes[timeframe] = {"bias": "NEUTRAL", "rsi": None, "macd_hist": None}
                continue
            rsi = float(row["rsi"] or 50)
            hist = float(row["macd_histogram"] or 0)
            bias = "BULLISH" if rsi >= 55 and hist > 0 else "BEARISH" if rsi <= 45 and hist < 0 else "NEUTRAL"
            score += 1 if bias == "BULLISH" else -1 if bias == "BEARISH" else 0
            timeframes[timeframe] = {"bias": bias, "rsi": rsi, "macd_hist": hist}
    abs_score = abs(score)
    level = "HIGH" if abs_score >= 3 else "MEDIUM" if abs_score >= 2 else "LOW"
    direction = "BUY" if score >= 2 else "SELL" if score <= -2 else "SKIP"
    return {
        "symbol": symbol.upper(),
        "timeframes": timeframes,
        "confluence_score": score,
        "confluence_level": level,
        "trade_direction": direction,
    }


@app.get("/api/scan/stage1/latest")
async def get_stage1_latest() -> dict[str, Any]:
    """Get latest Stage 1 scan results summary."""
    with get_db() as conn:
        # Get latest scan time
        latest_scan = conn.execute(
            "SELECT MAX(scan_time) FROM scan_results WHERE stage1_flagged = 1"
        ).fetchone()
        
        if not latest_scan or not latest_scan[0]:
            return {
                "scan_time": None,
                "total_scanned": 0,
                "total_flagged": 0,
                "flagged_stocks": [],
            }
        
        scan_time = latest_scan[0]
        
        # Get all flagged stocks from this scan
        rows = conn.execute("""
            SELECT symbol, stage1_price_change, stage1_volume_ratio, stage1_reason
            FROM scan_results
            WHERE scan_time = ? AND stage1_flagged = 1
            ORDER BY ABS(stage1_price_change) DESC
        """, (scan_time,)).fetchall()
        
        flagged_stocks = rows_to_dicts(rows)
        
        # Count reasons by user-facing category instead of every raw threshold value.
        reason_counts = {
            "price_up": 0,
            "price_down": 0,
            "volume_spike": 0,
            "fo_stock": 0,
            "open_trade": 0,
        }
        for stock in flagged_stocks:
            reasons = stock.get('stage1_reason', '').split(', ')
            for reason in reasons:
                if reason.startswith("price_+"):
                    reason_counts["price_up"] += 1
                elif reason.startswith("price_-"):
                    reason_counts["price_down"] += 1
                elif reason.startswith("volume_"):
                    reason_counts["volume_spike"] += 1
                elif reason == "fo_stock":
                    reason_counts["fo_stock"] += 1
                elif reason == "open_trade":
                    reason_counts["open_trade"] += 1
        reason_counts = {key: value for key, value in reason_counts.items() if value > 0}
        
        # Prefer stock_universe count; fall back to distinct candle
        # symbols so the count stays meaningful even when the universe
        # table has not been populated yet.
        total_scanned = 0
        if table_exists(conn, "stock_universe"):
            total_scanned = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_universe WHERE is_active = 1"
            ).fetchone()[0]
        if total_scanned == 0 and table_exists(conn, "candles"):
            total_scanned = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM candles"
            ).fetchone()[0]
        if total_scanned == 0:
            total_scanned = len(flagged_stocks)

        return {
            "scan_time": scan_time,
            "total_scanned": total_scanned,
            "total_flagged": len(flagged_stocks),
            "flagged_stocks": flagged_stocks[:50],  # Return top 50
            "reason_summary": reason_counts,
        }


@app.get("/api/scan/stage2/latest")
async def get_stage2_latest() -> dict[str, Any]:
    """Get latest Stage 2 scan results with scores."""
    with get_db() as conn:
        # Get latest scan time where stage2 data exists
        latest_scan = conn.execute(
            "SELECT MAX(scan_time) FROM scan_results WHERE stage = 2"
        ).fetchone()
        
        if not latest_scan or not latest_scan[0]:
            return {
                "scan_time": None,
                "total_analyzed": 0,
                "candidates_above_threshold": 0,
                "candidates": [],
            }
        
        scan_time = latest_scan[0]
        
        # Get all Stage 2 results
        rows = conn.execute("""
            SELECT 
                symbol, stage2_score, stage2_direction,
                stage2_pattern, stage2_setup, final_selected,
                stage1_price_change, stage1_volume_ratio
            FROM scan_results
            WHERE scan_time = ? AND stage = 2
            ORDER BY stage2_score DESC
        """, (scan_time,)).fetchall()
        
        candidates = rows_to_dicts(rows)
        candidates_selected = [c for c in candidates if c.get('final_selected') == 1]
        
        return {
            "scan_time": scan_time,
            "total_analyzed": len(candidates),
            "candidates_above_threshold": len(candidates_selected),
            "candidates": candidates_selected,
            "all_analyzed": candidates[:20],  # Top 20 by score
        }


@app.get("/api/scan/history")
async def get_scan_history() -> list[dict[str, Any]]:
    """Get scan results history for last 24 hours."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT 
                scan_time,
                COUNT(DISTINCT CASE WHEN stage1_flagged = 1 THEN symbol END) as stage1_count,
                COUNT(DISTINCT CASE WHEN stage = 2 THEN symbol END) as stage2_analyzed,
                COUNT(DISTINCT CASE WHEN final_selected = 1 THEN symbol END) as candidates
            FROM scan_results
            WHERE scan_time >= datetime('now', '-24 hours')
            GROUP BY scan_time
            ORDER BY scan_time DESC
        """).fetchall()
        
        return rows_to_dicts(rows)


@app.get("/api/top-picks")
async def top_picks() -> list[dict[str, Any]]:
    with get_db() as conn:
        return [s for s in get_signal_scores(conn) if s["direction"] != "HOLD"][:3]


@app.get("/api/trades/open")
async def open_trades() -> list[dict[str, Any]]:
    with get_db() as conn:
        return get_open_trades(conn)


@app.get("/api/trades/history")
async def trade_history() -> list[dict[str, Any]]:
    with get_db() as conn:
        return get_trade_history(conn)


@app.get("/api/strategy")
async def strategy() -> dict[str, Any] | None:
    with get_db() as conn:
        return get_active_strategy(conn)


@app.get("/api/strategy/history")
async def strategy_history() -> list[dict[str, Any]]:
    with get_db() as conn:
        from agents.learning_agent import ensure_learning_schema
        ensure_learning_schema(conn)
        rows = conn.execute(
            """
            SELECT s.*, i.win_rate AS learning_win_rate,
                   COALESCE(i.trades_analyzed, i.total_trades) AS trades_analyzed,
                   i.findings_json, i.summary
            FROM strategy_rules s
            LEFT JOIN learning_insights i ON i.strategy_version = s.version
            ORDER BY s.version DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.get("/api/learning/latest")
async def latest_learning_insight() -> dict[str, Any] | None:
    with get_db() as conn:
        from agents.learning_agent import ensure_learning_schema
        ensure_learning_schema(conn)
        row = conn.execute(
            "SELECT * FROM learning_insights ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


@app.post("/api/learning/run")
async def run_learning() -> dict[str, Any]:
    try:
        from agents.learning_agent import run_learning_cycle

        return await asyncio.to_thread(run_learning_cycle)
    except Exception as exc:
        return pipeline_error("learning", exc)


@app.post("/api/backtest/run")
async def run_backtest(request: BacktestRequest):
    try:
        from analysis.backtester import run

        return await asyncio.to_thread(
            run, request.date_from, request.date_to, request.symbol, request.strategy_version
        )
    except Exception as exc:
        return pipeline_error("backtest", exc)


@app.get("/api/backtest/history")
async def backtest_history() -> list[dict[str, Any]]:
    from analysis.backtester import ensure_backtest_schema

    with get_db() as conn:
        ensure_backtest_schema(conn)
        rows = conn.execute("SELECT * FROM backtest_runs ORDER BY created_at DESC, id DESC").fetchall()
    result = rows_to_dicts(rows)
    for item in result:
        try:
            item["details"] = json.loads(item.get("notes") or "{}")
        except json.JSONDecodeError:
            item["details"] = {}
    return result


@app.get("/api/backtest/{run_id}/trades")
async def backtest_trades(run_id: str) -> list[dict[str, Any]]:
    from analysis.backtester import ensure_backtest_schema

    with get_db() as conn:
        ensure_backtest_schema(conn)
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY entry_time, id", (run_id,)
        ).fetchall()
    return rows_to_dicts(rows)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_json(payload)


manager = ConnectionManager()


def websocket_snapshots() -> tuple[dict[str, Any], dict[str, Any]]:
    with get_db() as conn:
        signals_payload = {
            "type": "signal_update",
            "data": get_signal_scores(conn),
            "timestamp": iso_now(),
        }
        trades_payload = {
            "type": "trade_update",
            "data": get_open_trades(conn),
            "timestamp": iso_now(),
        }
    return signals_payload, trades_payload


@app.websocket("/ws/live")
async def live_updates(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        signal_payload, trade_payload = websocket_snapshots()
        await manager.send_json(websocket, signal_payload)
        await manager.send_json(websocket, trade_payload)

        while True:
            await asyncio.sleep(30)
            signal_payload, trade_payload = websocket_snapshots()
            await manager.send_json(websocket, signal_payload)
            await manager.send_json(websocket, trade_payload)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        await websocket.close()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=True)
