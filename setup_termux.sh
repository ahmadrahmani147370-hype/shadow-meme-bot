#!/usr/bin/env bash
# SHADOW MEME - Termux setup (Android). Safe to run more than once.
set -e
cd "$(dirname "$0")"

echo "== 1/6 Updating Termux packages =="
pkg update -y && pkg upgrade -y

echo "== 2/6 Python, git, numpy (prebuilt) =="
pkg install -y python git python-numpy tur-repo

echo "== 3/6 pandas (prebuilt from tur-repo) =="
pkg install -y python-pandas || echo "!! python-pandas from tur-repo failed - trying pip (slow, may compile)"
python -c "import pandas" 2>/dev/null || pip install pandas

echo "== 4/6 Small pure-Python libraries =="
# BUILD_EXTENSION=no -> websockets installs without compiling its optional C speedups.
BUILD_EXTENSION=no pip install --upgrade -r requirements.txt
pip install --upgrade pytest

echo "== 5/6 Optional: termux-api (battery status). Needs the Termux:API app from F-Droid too =="
pkg install -y termux-api || echo "   (skipped - optional)"

echo "== 6/6 Files and folders =="
mkdir -p data logs
[ -f .env ] || { cp .env.example .env; echo "   Created .env (fill it in later: nano .env)"; }
chmod +x start.sh

# Termux:Boot auto-start script (only used if the Termux:Boot app is installed from F-Droid).
BOOT="$HOME/.termux/boot"
mkdir -p "$BOOT"
cat > "$BOOT/start-shadow-meme.sh" <<BOOTEOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$(pwd)" && ./start.sh >> logs/boot.log 2>&1
BOOTEOF
chmod +x "$BOOT/start-shadow-meme.sh"

echo
echo "== Self-check =="
python -m shadow.doctor || true
echo
echo "Done. Next:"
echo "  1) Android Settings > Apps > Termux > Battery > Unrestricted (stops Android killing it)"
echo "  2) nano .env      (Telegram token + chat id - needed from phase 8)"
echo "  3) ./start.sh     (wake-lock + auto-restart; the Telegram bot itself arrives in phase 8)"
echo "  Optional: install Termux:Boot from F-Droid and open it once -> bot starts after phone reboot."
