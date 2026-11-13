#!/usr/bin/env bash
# Stop the box watchdog and the bot cleanly (SIGTERM -> bot saves state and exits).
cd "$(dirname "$0")/.." || exit 1
if [ -f logs/watchdog.pid ] && kill -0 "$(cat logs/watchdog.pid)" 2>/dev/null; then
  kill -TERM "$(cat logs/watchdog.pid)"; echo "watchdog $(cat logs/watchdog.pid) stopping"
else
  echo "no watchdog running"; [ -f logs/bot.pid ] && kill -TERM "$(cat logs/bot.pid)" 2>/dev/null && echo "bot stopped"
fi
for i in $(seq 1 30); do pgrep -f "python.*-u main.py" >/dev/null || break; sleep 1; done
pgrep -af "python.*-u main.py" || echo "nothing left running"
