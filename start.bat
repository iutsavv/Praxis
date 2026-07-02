@echo off
echo Starting AI Trading Agent...
start "Backend" cmd /k "cd backend && uvicorn main:app --reload --port 8000"
ping 127.0.0.1 -n 4 > nul
start "Frontend" cmd /k "cd frontend && npm.cmd run dev"
echo Dashboard: http://localhost:5173
echo API Docs:  http://localhost:8000/docs
