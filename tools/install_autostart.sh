#!/usr/bin/env bash
# Install / remove the SHADOW MEME autostart hook (v1.1).
#   bash tools/install_autostart.sh            install (or refresh)      --remove   uninstall
# This box has no cron daemon and no systemd (PID 1 is tini), so a real @reboot job is impossible.
# Instead a marked block at the TOP of ~/.bashrc and in ~/.profile runs tools/ensure_running.sh in the
# background whenever a shell starts (a terminal, a login, an agent session). ensure_running.sh is
# idempotent and silent, so this only (re)starts the bot when it is not running.
DIR=$(cd "$(dirname "$0")/.." && pwd)
BEGIN="# >>> shadow-meme autostart >>>"; END="# <<< shadow-meme autostart <<<"
BLOCK="$BEGIN
[ -x \"$DIR/tools/ensure_running.sh\" ] && ( \"$DIR/tools/ensure_running.sh\" >/dev/null 2>&1 & ) 2>/dev/null
$END"
strip() { [ -f "$1" ] && sed -i "/^$BEGIN\$/,/^$END\$/d" "$1"; }
chmod +x "$DIR/tools/ensure_running.sh" "$DIR/tools/box_run.sh" "$DIR/tools/box_stop.sh"
for f in "$HOME/.bashrc" "$HOME/.profile"; do
  strip "$f"
  [ "${1:-}" = "--remove" ] && { echo "removed from $f"; continue; }
  touch "$f"
  { printf '%s\n' "$BLOCK"; cat "$f"; } > "$f.shadow.tmp" && cat "$f.shadow.tmp" > "$f" && rm -f "$f.shadow.tmp"
  echo "installed in $f (top of file, runs for interactive and non-interactive shells)"
done
