#!/usr/bin/env bash
# Run SHADOW MEME on a Linux box/PC (not Termux) as a durable background process with a watchdog.
#   start:  setsid nohup bash tools/box_run.sh >/dev/null 2>&1 &      stop:  bash tools/box_stop.sh
# - bot output -> logs/box_run.log (rotated at 20 MB), watchdog events -> logs/watchdog.log
# - restarts the bot when it exits or crashes (10 s -> 300 s backoff), stops only on exit 2 (config error)
# - restarts it when the log has been silent for HANG_SEC (HEALTH lines arrive every HEALTH_LOG_SEC)
# - heartbeat in logs/watchdog.heartbeat; a gap much longer than the loop interval means the machine was
#   suspended/paused (logged as "box was suspended ~N s") - nothing can run while the box is paused.
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs data
rm -f logs/NO_AUTOSTART          # starting by hand re-enables the autostart hook
exec 9>&- 2>/dev/null || true    # never hold tools/ensure_running.sh's lock
# a shell-hook start may have a minimal PATH: keep python3 / gh (dashboard upload) reachable
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin"
# dashboard upload (DASHBOARD_PUBLISHER=gh): use the gh login. GH_TOKEN from the starting shell is inherited;
# without it, fall back to a stored `gh auth login` credential (never written to a file by this script)
if [ -z "${GH_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then GH_TOKEN=$(gh auth token 2>/dev/null) && [ -n "$GH_TOKEN" ] && export GH_TOKEN || unset GH_TOKEN; fi
# Use the token from .env, never a stale exported one (process env beats .env).
unset TELEGRAM_BOT_TOKEN TELEGRAM_BOT_TOKEN_NEW TG_BOT_TOKEN
export HEALTH_LOG_SEC=${HEALTH_LOG_SEC:-60}
PY=${PYTHON:-$(command -v python3 || command -v python)}
LOG=logs/box_run.log; WLOG=logs/watchdog.log; HB=logs/watchdog.heartbeat
HANG_SEC=${HANG_SEC:-600}; MAX_BYTES=${MAX_LOG_BYTES:-20000000}
echo $$ > logs/watchdog.pid
w() { echo "$(date '+%F %T %Z') $*" >> "$WLOG"; }
PID=""
stop_all() { w "watchdog stopping (signal)"; [ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null && wait "$PID" 2>/dev/null; rm -f logs/bot.pid logs/watchdog.pid; exit 0; }
trap stop_all INT TERM
delay=10
w "watchdog started (pid $$)"
while true; do
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$MAX_BYTES" ]; then mv -f "$LOG" "$LOG.1"; fi
  "$PY" -u main.py >> "$LOG" 2>&1 &
  PID=$!; echo "$PID" > logs/bot.pid; started=$(date +%s); last=$started
  w "bot started pid $PID"
  while kill -0 "$PID" 2>/dev/null; do
    sleep 15
    now=$(date +%s); echo "$now pid=$PID" > "$HB"
    gap=$(( now - last )); last=$now
    [ "$gap" -gt 90 ] && w "box was suspended/paused ~$((gap - 15)) s (bot pid $PID kept, resumes by itself)"
    age=$(( now - $(stat -c %Y "$LOG" 2>/dev/null || echo "$now") ))
    if [ "$gap" -le 90 ] && [ "$age" -gt "$HANG_SEC" ]; then
      w "no log output for ${age}s - bot looks hung, restarting"; kill -TERM "$PID" 2>/dev/null; sleep 20; kill -KILL "$PID" 2>/dev/null
    fi
  done
  wait "$PID"; code=$?; PID=""; rm -f logs/bot.pid
  w "bot exited with code $code after $(( $(date +%s) - started ))s"
  [ "$code" -eq 2 ] && { w "config error - fix .env; watchdog stops (autostart off until box_run.sh)"; touch logs/NO_AUTOSTART; rm -f logs/watchdog.pid; exit 2; }
  [ $(( $(date +%s) - started )) -gt 600 ] && delay=10
  w "restarting in ${delay}s"; sleep "$delay"; delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
done
