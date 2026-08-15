# Kronos Forecast Integration

Kronos is wired into Stage 2 as an optional price-forecast layer. It is disabled
by default so the normal paper-trading pipeline stays fast and does not require
PyTorch or Hugging Face model downloads.

## Install

Install the normal project dependencies first, then install the optional Kronos
dependencies:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-kronos.txt
```

The project expects the cloned Kronos repo at:

```text
E:\Ai trading agent\Kronos
```

## Enable

PowerShell:

```powershell
$env:ENABLE_KRONOS = "1"
$env:KRONOS_MODEL_ID = "NeoQuasar/Kronos-mini"
$env:KRONOS_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-2k"
$env:KRONOS_DEVICE = "cpu"
$env:KRONOS_INTERVAL = "1d"
$env:KRONOS_PRED_LEN = "15"
```

Then run Stage 2 or the orchestrator as usual:

```powershell
.\venv\Scripts\python.exe analysis\stage2_scanner.py
.\venv\Scripts\python.exe orchestrator\main.py --run-once --dry-run
```

## 15-Day Or 30-Day Forecast

Use daily candles:

```powershell
$env:KRONOS_INTERVAL = "1d"
$env:KRONOS_PRED_LEN = "15"
```

For 30 trading days:

```powershell
$env:KRONOS_PRED_LEN = "30"
```

## How It Changes Scoring

Stage 2 still uses the existing technical score first. Kronos then forecasts
future close price and calculates expected return percentage.

- If Kronos agrees with the Stage 2 direction, Stage 2 gets a positive score
  adjustment.
- If Kronos disagrees, Stage 2 gets a negative score adjustment.
- If Kronos is disabled, missing, or fails, Stage 2 continues with the original
  score.

Forecast details are saved in the `kronos_forecasts` table.

## Direct Test

Run a single forecast:

```powershell
$env:ENABLE_KRONOS = "1"
.\venv\Scripts\python.exe analysis\kronos_forecaster.py RELIANCE --direction BUY --pred-len 15
```

Use this before enabling Kronos in continuous orchestrator runs.
