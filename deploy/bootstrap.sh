#!/usr/bin/env bash
# Flowstate Alpha real-time worker — one-shot setup for a fresh Ubuntu VM
# (Oracle Cloud Always Free ARM/AMD, GCP e2-micro, or any always-on Linux box).
#
# Usage:
#   sudo git clone https://github.com/Chasingpow/flowstate-earnings-bot /opt/flowstate-earnings-bot
#   cd /opt/flowstate-earnings-bot/deploy
#   sudo bash bootstrap.sh
#   sudo nano /opt/flowstate-earnings-bot/.env      # paste FMP + Discord secrets
#   sudo systemctl restart flowstate-worker
#   journalctl -u flowstate-worker -f               # watch it live
set -euo pipefail

REPO="/opt/flowstate-earnings-bot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# if run from inside a differently-located checkout, use that as the repo root
if [ -f "$SCRIPT_DIR/../worker.py.gz.b64" ] || [ -f "$SCRIPT_DIR/../worker.py" ]; then REPO="$(cd "$SCRIPT_DIR/.." && pwd)"; fi
echo "==> repo: $REPO"

# worker.py is shipped packed (worker.py.gz.b64) to survive transport; unpack it.
if [ -f "$REPO/worker.py.gz.b64" ]; then
  echo "==> unpacking worker.py from worker.py.gz.b64"
  base64 -d "$REPO/worker.py.gz.b64" | gunzip > "$REPO/worker.py"
  python3 -c "import ast,sys; ast.parse(open('$REPO/worker.py').read()); print('   worker.py OK')" \
    || { echo '!! worker.py failed to unpack/parse'; exit 1; }
fi

echo "==> installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip git curl ca-certificates fontconfig chromium-browser || \
  apt-get install -y python3 python3-pip git curl ca-certificates fontconfig chromium

# locate the chromium binary (apt deb, snap, or transitional)
CHROME=""
for c in chromium-browser chromium /snap/bin/chromium /usr/bin/chromium-browser /usr/bin/chromium; do
  if command -v "$c" >/dev/null 2>&1; then CHROME="$(command -v "$c")"; break; fi
  if [ -x "$c" ]; then CHROME="$c"; break; fi
done
if [ -z "$CHROME" ]; then
  echo "!! chromium not found via apt; installing via snap"
  snap install chromium || true
  CHROME="/snap/bin/chromium"
fi
echo "==> chromium: $CHROME"

echo "==> python deps"
pip3 install --no-input -r "$REPO/requirements.txt"

echo "==> fonts (Inter + Space Grotesk from the fontsource CDN)"
mkdir -p "$REPO/fonts"
dl() { curl -fsSL "$1" -o "$2" && echo "   got $(basename "$2")" || echo "   (skip $(basename "$2"))"; }
BASE="https://cdn.jsdelivr.net/npm"
dl "$BASE/@fontsource/inter/files/inter-latin-400-normal.woff2" "$REPO/fonts/inter-400.woff2"
dl "$BASE/@fontsource/inter/files/inter-latin-500-normal.woff2" "$REPO/fonts/inter-500.woff2"
dl "$BASE/@fontsource/inter/files/inter-latin-600-normal.woff2" "$REPO/fonts/inter-600.woff2"
dl "$BASE/@fontsource/inter/files/inter-latin-700-normal.woff2" "$REPO/fonts/inter-700.woff2"
dl "$BASE/@fontsource/inter/files/inter-latin-800-normal.woff2" "$REPO/fonts/inter-800.woff2"
dl "$BASE/@fontsource/space-grotesk/files/space-grotesk-latin-500-normal.woff2" "$REPO/fonts/grotesk-500.woff2"
dl "$BASE/@fontsource/space-grotesk/files/space-grotesk-latin-700-normal.woff2" "$REPO/fonts/grotesk-700.woff2"

echo "==> .env"
if [ ! -f "$REPO/.env" ]; then
  cp "$REPO/deploy/.env.example" "$REPO/.env"
  sed -i "s#^CHROME_BIN=.*#CHROME_BIN=$CHROME#" "$REPO/.env"
  chmod 600 "$REPO/.env"
  echo "   created $REPO/.env — EDIT IT and paste FMP_API_KEY + DISCORD_WEBHOOK_URL"
else
  echo "   $REPO/.env already exists; leaving it"
fi
mkdir -p "$REPO/state"

echo "==> systemd service"
install -m 644 "$REPO/deploy/flowstate-worker.service" /etc/systemd/system/flowstate-worker.service
systemctl daemon-reload
systemctl enable flowstate-worker
echo ""
echo "==> DONE. Next:"
echo "   1) sudo nano $REPO/.env        # paste your FMP + Discord secrets"
echo "   2) sudo systemctl restart flowstate-worker"
echo "   3) journalctl -u flowstate-worker -f   # watch it"
echo ""
echo "   Tip: set SELFTEST_TICKER=AAPL in .env once, restart, and it will post"
echo "   one card immediately so you can confirm rendering + posting work."
