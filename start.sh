#!/bin/bash
# Start both backend and frontend together

echo "Starting AI Trading Agent..."

# Start backend
cd backend
uvicorn main:app --reload --port 8000 &
BACKEND_PID=$!
echo "Backend started (PID: $BACKEND_PID)"

# Start frontend
cd ../frontend
npm run dev &
FRONTEND_PID=$!
echo "Frontend started (PID: $FRONTEND_PID)"

echo ""
echo "Dashboard: http://localhost:5173"
echo "API docs:  http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both servers"

# Kill both on exit
trap "kill $BACKEND_PID $FRONTEND_PID" EXIT
wait
