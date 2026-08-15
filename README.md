# AI Paper Trading Agent for NSE Stocks

An end-to-end **paper-trading** platform for National Stock Exchange (NSE) equities. It collects market data, narrows the NSE universe through a two-stage scanner, scores technical setups, applies risk and validation controls, records simulated trades, and presents results in a React dashboard.

> **Educational use only — not investment advice.** This project does not send broker orders, but signals, forecasts, backtests, and performance metrics can be wrong. Do not use them as the basis for real-money trades without independent research, testing, and appropriate professional advice.

## What it does

- Fetches NSE OHLCV data from Yahoo Finance through yfinance.
- Runs a two-stage scanner: a rapid broad screen followed by deep technical analysis.
- Calculates RSI, MACD, EMA, VWAP, and volume-ratio indicators.
- Incorporates multi-timeframe confluence, support/resistance, patterns, announcements, and news sentiment.
- Determines market context as TRENDING, SIDEWAYS, or VOLATILE.
- Validates and sizes paper trades, then monitors targets, stops, end-of-day exits, and trade replacements.
- Provides FastAPI REST/WebSocket endpoints, a React dashboard, backtesting, and weekly learning summaries.

## Architecture

~~~text
Yahoo Finance / NSE sources
          |
          v
  Data fetchers --> SQLite database <-- Announcements / news
          |                 |
          v                 v
Indicators + support/resistance + sentiment
          |
          v
Stage 1 broad scan --> Stage 2 deep analysis --> Candidate picks
                                                    |
                                                    v
                                   Validator + risk manager + paper trader
                                                    |
                                                    v
                                  FastAPI / WebSocket --> React dashboard
~~~

## Technology stack

| Area | Technology |
| --- | --- |
| Backend API | FastAPI, Uvicorn, WebSockets |
| Frontend | React 19, TypeScript, Vite, Recharts |
| Scheduling | APScheduler in Asia/Kolkata timezone |
| Storage | SQLite (database/trading.db) |
| Data & analysis | pandas, yfinance, rule-based indicators |
| Sentiment | feedparser, spaCy, NLTK, VADER |
| Optional forecasting | Kronos / PyTorch dependencies |

## Requirements

- Python 3.11 or newer
- Node.js 18 or newer with npm
- Internet access for market data and news

All commands below assume the repository root. Windows PowerShell is shown; on macOS/Linux use python3 and source venv/bin/activate.

## Quick start

### 1. Prepare Python and the database

~~~powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python database\init_db.py
~~~

If PowerShell blocks activation, use the virtual-environment interpreter directly:

~~~powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe database\init_db.py
~~~

Database initialization is safe to run again; it creates missing tables and seeds defaults without intentionally resetting the paper account.

### 2. Install dashboard dependencies

~~~powershell
cd frontend
npm install
cd ..
~~~

### 3. Run a safe smoke test

This is the recommended first command. It runs one pipeline cycle outside normal market hours if necessary, but **does not open or close paper trades**.

~~~powershell
.\venv\Scripts\python.exe orchestrator\main.py --run-once --dry-run
~~~

### 4. Start the application

Use three terminals so each service can be monitored independently.

**Terminal 1 — API**

~~~powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
~~~

**Terminal 2 — dashboard**

~~~powershell
cd frontend
npm run dev
~~~

**Terminal 3 — scheduler (start in dry-run mode)**

~~~powershell
.\venv\Scripts\python.exe orchestrator\main.py --dry-run
~~~

- Dashboard: <http://localhost:5173>
- API documentation: <http://localhost:8000/docs>
- Basic API check: <http://localhost:8000/api/ping>

## Operating modes

| Command | Result |
| --- | --- |
| python orchestrator/main.py --run-once --dry-run | One safe diagnostic cycle; bypasses the scheduler market-hours guard. |
| python orchestrator/main.py --dry-run | Continuous scanning and validation; paper positions are never opened or closed. |
| python orchestrator/main.py --run-once | One full paper-trading cycle; use only after reviewing dry-run output. |
| python orchestrator/main.py | Continuous scheduler with simulated trade execution enabled. |

The trade validator keeps its own market-hours and safety checks. Dry-run mode can fetch live data and write scan results, but it does not change paper-trade positions.

## Scheduled workflow

All schedules use **Asia/Kolkata (IST)**. The scheduler runs an immediate startup cycle, then runs weekday jobs. The ordinary market cycle is guarded to operate from 09:15 to 15:25 IST.

| Job | Frequency / time | Purpose |
| --- | --- | --- |
| Market pipeline | Every 5 minutes, weekdays | Fetches 5m/15m candles, calculates indicators, scans, validates picks, and monitors trades. |
| Hourly candle refresh | Every 30 minutes | Fetches 1h candles and indicators. |
| Daily candle refresh | 09:00 | Fetches 1d candles and indicators. |
| Announcements / levels | Every 30 minutes | Updates corporate announcements and support/resistance levels. |
| News fetch | Every 15 minutes | Retrieves RSS news. |
| Sentiment processing | Every 5 minutes | Scores unprocessed news locally. |
| EOD monitoring | 15:30 | Closes remaining paper positions through the monitoring path. |
| Learning cycle | Saturday 10:00 | Produces strategy-performance insights. |
| Universe refresh | Sunday 09:00 | Refreshes the stock universe. |

The orchestrator prevents overlapping cycles, skips the following scheduled cycle if the prior one exceeds four minutes, and disables a repeatedly failing stage for the rest of the session after three consecutive failures.

## Trading and risk controls

A proposed position must pass checks such as valid direction, score threshold, no duplicate position, available capital, maximum open positions, allowed market hours, and optional sideways-market restrictions.

Default seeded settings:

| Setting | Default |
| --- | --- |
| Starting paper balance | Rs. 100,000 |
| Minimum score to trade | 60 |
| Maximum open trades (strategy rule) | 3 |
| Risk per trade | 1% of balance |
| Indicator weights | RSI / MACD / volume / VWAP: 25% each |
| Position capital cap | 20% of current balance |
| Stop-loss distance | 0.75% from entry |
| Target distance | 1.5% from entry |

At orchestration time, capacity can vary with market condition: 8 trades in TRENDING, 6 in SIDEWAYS, and 4 in VOLATILE. A full book may replace a losing, lower-confidence position only when a new strong candidate has a sufficient score advantage. These are implementation defaults, not claims of suitability or profitability.

## Project layout

~~~text
agents/          Paper trading, validation, risk, learning, and performance logic
analysis/        Indicators, scanners, signals, patterns, backtesting, forecasts
backend/         FastAPI routes and WebSocket service
data/            Candle, universe, announcement, and news fetchers
database/         SQLite initialization and migrations
docs/             Feature-specific technical guides
frontend/         React dashboard
orchestrator/    APScheduler pipeline and health status
tests/            Connection, end-to-end, and feature tests
logs/             Generated runtime output
~~~

## Data pipeline

1. **Ingestion** — active symbols and Nifty are fetched at 5m, 15m, 1h, and 1d intervals as applicable. New symbols backfill available history; later runs fetch incrementally.
2. **Feature generation** — indicator values are stored in SQLite. Support/resistance, patterns, announcements, and sentiment provide context.
3. **Stage 1 broad scan** — movement, volume, F&O membership, and open positions identify symbols for deeper analysis.
4. **Stage 2 deep scan** — recent symbols are scored for technical setup, direction, market context, and timeframe confluence. A candidate must be non-HOLD, meet the active score threshold, and pass confluence checks.
5. **Validation and sizing** — accepted candidates receive a quantity, stop-loss, target, and capital requirement.
6. **Paper trading and monitoring** — approved positions are recorded and monitored for target, stop-loss, replacement, or EOD exit.
7. **Reporting** — the API and dashboard expose account, trades, scans, news, pipeline, and backtest data.

## API and dashboard

The complete interactive API contract is at <http://localhost:8000/docs> while the backend is running.

| Endpoint | Description |
| --- | --- |
| GET /api/ping | Basic API availability check. |
| GET /api/health | Backend and orchestrator health summary. |
| GET /api/account/summary | Paper-account balance and performance. |
| GET /api/pipeline/status | Latest pipeline and scheduler state. |
| POST /api/pipeline/run-full-cycle | Runs an on-demand pipeline cycle. |
| GET /api/scan/latest | Latest scan overview. |
| GET /api/scan/stage1/latest | Latest Stage 1 flagged symbols. |
| GET /api/scan/stage2/latest | Latest Stage 2 results and candidates. |
| GET /api/top-picks | Current top trade candidates. |
| GET /api/trades/open | Open paper positions. |
| GET /api/trades/history | Closed and historical paper trades. |
| POST /api/backtest/run | Starts a historical backtest. |
| WS /ws/live | Live dashboard updates. |

## Configuration

### Strategy and scoring

- analysis/signal_config.json stores indicator thresholds and market-condition settings.
- The active SQLite strategy_rules row controls score weights, minimum trade score, maximum positions, risk per trade, entry window, and sideways-market behavior.
- agents/risk_manager.py contains implementation-level sizing caps and price distances.

Make changes deliberately, retain a record of earlier values, and test in --dry-run mode first. Direct SQLite edits while services are running can create locking contention.

### Optional Kronos forecasts

Kronos is disabled by default, so normal operation does not need its model dependencies. To install and enable it, follow [the Kronos integration guide](docs/KRONOS_INTEGRATION.md). If Kronos is unavailable or fails, Stage 2 continues with its base technical score.

### Frontend API URL

The frontend targets the local API by default. To use a different origin, set VITE_API_BASE_URL before starting Vite:

~~~powershell
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
cd frontend
npm run dev
~~~

## Manual commands

~~~powershell
# Populate candles and calculate indicators
.\venv\Scripts\python.exe data\fetcher.py
.\venv\Scripts\python.exe analysis\indicator_engine.py

# Run the conventional signal scan
.\venv\Scripts\python.exe analysis\signal_engine.py

# Exercise the complete two-stage scan
.\venv\Scripts\python.exe test_two_stage_pipeline.py

# Run a single optional Kronos forecast after enabling it
.\venv\Scripts\python.exe analysis\kronos_forecaster.py RELIANCE --direction BUY --pred-len 15
~~~

For scanner-specific details, see [the two-stage scanning guide](docs/TWO_STAGE_SCANNING.md).

## Testing and build verification

~~~powershell
# Python tests
.\venv\Scripts\python.exe -m pytest tests

# Legacy ordered end-to-end checks (may fetch data)
.\venv\Scripts\python.exe tests\test_all.py

# Frontend production build
cd frontend
npm run build
~~~

Some tests and fetch commands rely on live data. Results can differ when markets are closed, upstream data is delayed, or provider limits apply.

## Runtime files and observability

| Location | Contents |
| --- | --- |
| logs/orchestrator.log | Rotating orchestrator log. |
| orchestrator/status.json | Latest cycle status, counters, duration, and balance. |
| database/trading.db | Main SQLite database. |
| logs/backend*.log, logs/frontend*.log | Service output, when created locally. |

Do not commit runtime logs, SQLite WAL/SHM files, or a database containing results you do not intend to share. Back up database/trading.db before schema migrations or substantial strategy experimentation.

## Troubleshooting

### The dashboard cannot reach the backend

Confirm <http://localhost:8000/api/ping> responds. Ensure the API runs on port 8000, restart the frontend, and set VITE_API_BASE_URL if you changed the API address.

### The orchestrator appears stopped

Start it in a separate terminal and inspect orchestrator/status.json and logs/orchestrator.log. The dashboard considers a running status with a recent last_run healthy.

### No scans, candidates, or trades appear

Initialize the database and run the safe one-cycle dry run. First fetches take longer because they obtain history. Candidates also require recent data, a non-HOLD direction, a qualifying score, and timeframe confluence; a valid scan can produce no picks.

### ModuleNotFoundError

Use the virtual-environment Python and reinstall dependencies:

~~~powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

### Database is locked

Do not run multiple writers (scheduler, manual fetcher, or database utilities) at once. Stop duplicate processes with Ctrl+C, wait for SQLite activity to settle, then restart only the needed service.

### A port is already in use

Stop the existing backend/frontend process or select a different port. Update VITE_API_BASE_URL when changing the backend port.

## Stopping the application

Press Ctrl+C in each service terminal. The orchestrator completes an active cycle before writing status: stopped and printing its session summary.

## Related documentation

- [Two-stage scanning guide](docs/TWO_STAGE_SCANNING.md)
- [Kronos forecast integration](docs/KRONOS_INTEGRATION.md)
- [Trade replacement summary](docs/FIX1_TRADE_REPLACEMENT_SUMMARY.md)
- [Trade replacement quick reference](docs/FIX1_QUICK_REFERENCE.md)
