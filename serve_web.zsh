#!/usr/bin/env zsh
# serve_web.zsh — Material Design Web UI for VoxCPM Narrate
#
#   ./serve_web.zsh
#   VOXCPM_WEB_PORT=8080 ./serve_web.zsh
#
# Open http://127.0.0.1:7860

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Installing…" >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

export VOXCPM_WEB_HOST="${VOXCPM_WEB_HOST:-127.0.0.1}"
export VOXCPM_WEB_PORT="${VOXCPM_WEB_PORT:-7860}"
export VOXCPM_WEB_OUT="${VOXCPM_WEB_OUT:-$SCRIPT_DIR/output/voxcpm2/web_jobs}"

echo "==> uv sync"
uv sync

echo "==> VoxCPM Narrate Web UI"
echo "    http://${VOXCPM_WEB_HOST}:${VOXCPM_WEB_PORT}"
echo "    jobs dir: ${VOXCPM_WEB_OUT}"
echo

uv run voxcpm-narrate-web
