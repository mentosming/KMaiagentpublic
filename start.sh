#!/usr/bin/env bash

echo "🚀 Starting Nexus-OS services..."

# Start Telegram Bot in the background (Force polling to prevent port conflict with FastAPI)
# Wrap in a while loop to auto-restart on 'Conflict' error during zero-downtime deployments
(
  while true; do
    WEBHOOK_URL="" python telegram_bot.py
    echo "⚠️ telegram_bot.py crashed or stopped. Restarting in 5 seconds..."
    sleep 5
  done
) &

# Start FastAPI server in the foreground
# Wait for the port environment variable (Zeabur uses PORT), default to 8000
PORT=${PORT:-8000}
echo "🌐 Starting FastAPI server on port $PORT..."
uvicorn main:app --host 0.0.0.0 --port $PORT
