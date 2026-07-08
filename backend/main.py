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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
    row = conn.execute(
        """
        SELECT id, symbol, scan_cycle, confidence_score, direction,
               market_condition, contrib_rsi, contrib_macd, contrib_volume,
               contrib_vwap, explanation, created_at
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
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, MAX(scan_cycle) AS scan_cycle
            FROM signal_scores
            GROUP BY symbol
        )
        SELECT s.id, s.symbol, s.scan_cycle, s.confidence_score, s.direction,
               s.market_condition, s.contrib_rsi, s.contrib_macd,
               s.contrib_volume, s.contrib_vwap, s.explanation, s.created_at
        FROM signal_scores s
        JOIN latest l ON l.symbol = s.symbol AND l.scan_cycle = s.scan_cycle
        ORDER BY s.confidence_score DESC, s.symbol ASC
        """
    ).fetchall()

    if rows:
        return rows_to_dicts(rows)

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
