#!/usr/bin/env bash
# SHADOW MEME - Termux setup (Android). Safe to run more than once.
# Only pure-Python packages are installed: nothing here needs a compiler (no Rust, no C builds).
# Run it with:  bash setup_termux.sh
set -e
cd "$(dirname "$0")"

echo "== 1/5 Updating Termux packages =="
pkg update -y && pkg upgrade -y

echo "== 2/5 Python and git =="
pkg install -y python git

echo "== 3/5 Small pure-Python libraries (httpx, websockets, python-dotenv, pytest) =="
# BUILD_EXTENSION=no -> websockets installs without compiling its optional C speedups.
BUILD_EXTENSION=no pip install --upgrade -r requirements.txt
pip install --upgrade pytest
# pandas/numpy are NOT needed by the bot (only an optional analysis helper). If you want them, use the prebuilt
# Termux packages - never `pip install pandas` on the phone (it tries to compile for a long time):
#   pkg install tur-repo && pkg install python-numpy python-pandas

echo "== 4/5 Optional: termux-api (battery status). Needs the Termux:API app from F-Droid too =="
pkg install -y termux-api || echo "   (skipped - optional)"

echo "== 5/5 Files and folders =="
mkdir -p data logs
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "   Created .env (put your Telegram token in it: nano .env)"; }
chmod +x start.sh

# Termux:Boot auto-start script (only used if the Termux:Boot app is installed from F-Droid).
BOOT="$HOME/.termux/boot"
mkdir -p "$BOOT"
cat > "$BOOT/start-shadow-meme.sh" <<BOOTEOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$(pwd)" && bash ./start.sh >> logs/boot.log 2>&1
BOOTEOF
chmod +x "$BOOT/start-shadow-meme.sh"

echo
echo "== Self-check =="
python -m shadow.doctor || true
echo
echo "Done. Next:"
echo "  1) Android Settings > Apps > Termux > Battery > Unrestricted (stops Android killing it)"
echo "  2) nano .env      (TELEGRAM_BOT_TOKEN=...; chat id 7922990181 is already set)"
echo "  3) Stop the old FATHER bot (one token = one program), then: bash start.sh"
echo "  Optional: GITHUB_TOKEN in .env for the full dashboard; Termux:Boot (F-Droid) to start after a reboot."
