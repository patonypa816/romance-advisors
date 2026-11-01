#!/bin/sh
# Task-local dependencies only. Never source .env or start Studio/cloud services.
set -eu
cd "$(dirname "$0")/.."
with_node=0
case "${1:-}" in
    "") ;;
    --with-legacy-node) with_node=1 ;;
    *) echo "Usage: sh tools/prepare_cloud.sh [--with-legacy-node]" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || exit 2
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "Python 3.11 or newer is required (asyncio.timeout)." >&2
    exit 1
}
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "Existing .venv requires Python 3.11 or newer; preserve it and recreate explicitly." >&2
    exit 1
}
.venv/bin/python -m pip install -r requirements.txt
sh tools/fetch_vendor.sh
if [ "$with_node" -eq 1 ]; then
    command -v npm >/dev/null 2>&1 || { echo "npm is required for the optional historical helper." >&2; exit 1; }
    (cd studio/sdk && npm ci --ignore-scripts --no-audit --no-fund)
fi
for tool in ffmpeg ffprobe; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Not installed: $tool (required for audio conversion, not credential unit tests)."
    fi
done
echo "Task-local preparation complete; no server or cloud session started."
