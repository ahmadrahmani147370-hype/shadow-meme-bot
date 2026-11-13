#!/usr/bin/env bash
# Start SHADOW MEME with wake-lock, auto-restart after crashes and simple log rotation.
# Runs everything (scanner, signals, paper trading, Telegram). Control is Telegram only.
# Stops (no restart) on exit 0 (normal stop), 2 (config error) or 4 (another SHADOW MEME already runs on this DB).
cd "$(dirname "$0")"
mkdir -p logs data
PY=${PYTHON:-$(command -v python || command -v python3)}
LOG=logs/stdout.log
MAX_BYTES=${MAX_LOG_BYTES:-5000000}
command -v termux-wake-lock >/dev/null && termux-wake-lock
PID=""
cleanup() {
  if [ -n "$PID" ]; then kill -TERM "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; fi
  command -v termux-wake-unlock >/dev/null && termux-wake-unlock
  exit 0
}
trap cleanup INT TERM

delay=10
while true; do
  # rotate: stdout.log -> .1 -> .2 -> .3
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$MAX_BYTES" ]; then
    for i in 2 1; do [ -f "$LOG.$i" ] && mv "$LOG.$i" "$LOG.$((i+1))"; done
    mv "$LOG" "$LOG.1"
  fi
  started=$(date +%s)
  "$PY" main.py >> "$LOG" 2>&1 &
  PID=$!
  echo "$(date '+%F %T') started SHADOW MEME pid $PID"
  wait "$PID"
  code=$?
  PID=""
  case "$code" in
    0) echo "$(date '+%F %T') bot exited normally."; break ;;
    2) echo "Config error - see $LOG, fix .env and run again."; break ;;
    4) echo "Another SHADOW MEME is already running on this database - stop it first."; break ;;
  esac
  # crash: back off 10s -> 20s -> ... max 300s; reset if it ran for 10+ minutes
  [ $(( $(date +%s) - started )) -gt 600 ] && delay=10
  echo "$(date '+%F %T') bot stopped (exit $code). Restarting in ${delay}s (Ctrl+C to quit)"
  sleep "$delay"
  delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
done
command -v termux-wake-unlock >/dev/null && termux-wake-unlock
