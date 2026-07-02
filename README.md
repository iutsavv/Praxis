# AI Paper Trading Agent for NSE Stocks

An end-to-end paper-trading application for NSE stocks. It fetches 15-minute candles, calculates technical indicators, scores signals, validates and sizes proposed positions, manages paper trades, and displays account and pipeline state in a React dashboard.

> This project performs paper trading only. Start with dry-run mode and review the generated signals before enabling paper-trade execution.

## Components

- **Backend:** FastAPI API and WebSocket service on port `8000`
- **Frontend:** React + Vite dashboard on port `5173`
- **Orchestrator:** APScheduler process that runs the trading pipeline every 15 minutes during NSE market hours
- **Database:** SQLite at `database/trading.db`

## Prerequisites

- Python 3.11 or newer
- Node.js 18 or newer and npm
- Internet access for NSE market data through Yahoo Finance

All commands below should be run from the project root.

## First-time setup (Windows PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

cd frontend
npm install
cd ..

python database\init_db.py
```

If PowerShell blocks activation, either allow local scripts for your user or call the virtualenv's Python directly:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe database\init_db.py
```

## Run the application

Use three terminals so each service can be stopped and inspected independently.

### 1. Start the backend

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

### 2. Start the frontend

```powershell
cd frontend
npm run dev
```

### 3. Start the orchestrator safely

Recommended first run:

```powershell
.\venv\Scripts\python.exe orchestrator\main.py --run-once --dry-run
```

After confirming that the one-cycle logs are correct, run it continuously without opening or closing trades:

```powershell
.\venv\Scripts\python.exe orchestrator\main.py --dry-run
```

When ready to enable paper-trade execution:

```powershell
.\venv\Scripts\python.exe orchestrator\main.py
```

The continuous orchestrator runs an immediate startup cycle, then runs every 15 minutes from 09:15 through 15:15 IST, Monday through Friday. A dedicated 15:30 IST cycle closes remaining paper positions with `EOD_EXIT`.

## URLs

- Dashboard: <http://localhost:5173>
- API documentation: <http://localhost:8000/docs>
- Backend ping: <http://localhost:8000/api/ping>
- Orchestrator health: <http://localhost:8000/api/health>

## Orchestrator modes

| Command | Behavior |
| --- | --- |
| `python orchestrator/main.py` | Continuous scheduler with paper-trade execution |
| `python orchestrator/main.py --run-once` | One immediate cycle, ignoring the orchestrator market-hours guard |
| `python orchestrator/main.py --dry-run` | Continuous scan and validation without opening or closing trades |
| `python orchestrator/main.py --run-once --dry-run` | Safest one-cycle smoke test |

The validator retains its own safety rules, including its market-hours check, even when `--run-once` bypasses the orchestrator's initial guard.

## Logs and status

- Rotating log: `logs/orchestrator.log`
- Current health/status: `orchestrator/status.json`
- SQLite database: `database/trading.db`

The dashboard reports the orchestrator as live when its status is `running` and `last_run` is less than 20 minutes old.

## Stop the application

Press `Ctrl+C` in each terminal. The orchestrator waits for an active cycle to finish, writes `status: stopped`, and prints a session summary before exiting.

## Tests and builds

```powershell
.\venv\Scripts\python.exe -m pytest tests

cd frontend
npm run build
```

## Troubleshooting

### The dashboard says the backend is unavailable

Confirm that <http://localhost:8000/api/ping> responds and that port `8000` is not already occupied.

### The dashboard says `Orchestrator: Stopped`

Start the orchestrator in a separate terminal. If it is already running, inspect `logs/orchestrator.log` and `orchestrator/status.json` for the last cycle and failure counters.

### `ModuleNotFoundError`

Make sure commands use the virtualenv Python and reinstall dependencies:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

### No signals or trades appear

Initialize the database, confirm internet connectivity, and run one dry cycle. The first fetch may take longer because it backfills candle history.

### Port already in use

Stop the existing backend/frontend process or launch on a different port. If the frontend API port changes, set `VITE_API_BASE_URL` before running Vite.
