#!/usr/bin/env bash
# Idempotent "keep SHADOW MEME running" check (v1.1). Safe to call as often as you like:
#   - does nothing when the watchdog is alive (cheap: one kill -0)
#   - starts tools/box_run.sh (watchdog + bot) when it is not, e.g. after a box restart
#   - an orphaned bot (watchdog gone) is stopped first, so there is never a second bot on the same DB
#   - serialised with flock, so many shells opening at once start it only once
#   - respects logs/NO_AUTOSTART (created by box_stop.sh) and a config error (exit 2) marker
# Called from the ~/.bashrc / ~/.profile hook installed by tools/install_autostart.sh. Prints nothing
# unless -v is given.
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
V=0; [ "${1:-}" = "-v" ] && V=1
say() { [ "$V" = 1 ] && echo "$*"; echo "$(date '+%F %T %Z') ensure_running: $*" >> logs/watchdog.log; }
exec 9>logs/.ensure.lock
flock -n 9 || { [ "$V" = 1 ] && echo "another ensure_running is busy"; exit 0; }
if [ -f logs/NO_AUTOSTART ]; then [ "$V" = 1 ] && echo "autostart disabled (logs/NO_AUTOSTART; box_stop.sh made it) - start with box_run.sh"; exit 0; fi
if [ -f logs/watchdog.pid ] && kill -0 "$(cat logs/watchdog.pid)" 2>/dev/null \
   && grep -q box_run.sh "/proc/$(cat logs/watchdog.pid)/cmdline" 2>/dev/null; then
  [ "$V" = 1 ] && echo "running: watchdog $(cat logs/watchdog.pid), bot $(cat logs/bot.pid 2>/dev/null || echo '?')"
  exit 0
fi
ROOT=$(pwd)
for p in $(pgrep -f "python.*-u main.py" || true); do
  if [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$ROOT" ]; then
    say "orphan bot pid $p without watchdog - stopping it before restart"; kill -TERM "$p" 2>/dev/null
    for i in $(seq 1 25); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    kill -KILL "$p" 2>/dev/null
  fi
done
say "watchdog not running - starting tools/box_run.sh"
setsid nohup bash tools/box_run.sh >/dev/null 2>&1 < /dev/null 9>&- &     # 9>&-: the watchdog must not keep the lock
sleep 2
[ "$V" = 1 ] && echo "started: watchdog $(cat logs/watchdog.pid 2>/dev/null || echo '?')"
exit 0
