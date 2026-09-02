#!/usr/bin/env bash
#
# cosmos_describe.sh — record a few seconds from the Reachy camera and have
# NVIDIA Cosmos Reason 2 describe the scene, running 100% locally on the Jetson
# Thor GPU. NO Strands agent, NO AWS/Bedrock tokens.
#
# Usage:
#   ./cosmos_describe.sh                          # 5s clip, default prompt
#   ./cosmos_describe.sh --seconds 8 --fps 4
#   ./cosmos_describe.sh --prompt "What is the person doing?" --show-thinking
#
# First run downloads the model (~5GB) and the CUDA PyTorch wheel; later runs are fast.

set -uo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv-cosmos}"        # dedicated venv (heavy CUDA stack)
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"     # cu130 wheels are cp312
JETSON_INDEX="https://pypi.jetson-ai-lab.io/sbsa/cu130/"
DAEMON_PORT="${DAEMON_PORT:-8000}"

log() { printf '\033[1;36m[cosmos]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[cosmos]\033[0m %s\n' "$*" >&2; }

command -v uv >/dev/null 2>&1 || { log "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -d "$VENV_DIR" ] || { log "Creating venv ($VENV_DIR, py$PYTHON_VERSION)..."; uv venv "$VENV_DIR" --python "$PYTHON_VERSION"; }
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Install only if torch+CUDA isn't already working (keeps repeat runs instant).
if ! python -c "import torch,transformers,cv2,qwen_vl_utils; assert torch.cuda.is_available()" 2>/dev/null; then
  log "Installing CUDA PyTorch (Jetson cu130) + Cosmos deps (first run, large)..."
  uv pip install torch==2.11.0 --index-url "$JETSON_INDEX" \
    --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match || { err "torch install failed"; exit 1; }
  # torchvision 0.25.0 over-pins torch==2.10; install without deps to keep torch 2.11.
  uv pip install torchvision==0.25.0 --no-deps --index-url "$JETSON_INDEX" || err "torchvision install issue (continuing)"
  uv pip install transformers accelerate qwen-vl-utils opencv-python pillow numpy || { err "deps install failed"; exit 1; }
fi

# --- Pop the head up so the camera has a forward view ----------------------
# The head rests down (camera pointed away). A --no-media daemon started WITH
# wake-up-on-start raises the head to upright on startup by itself (no SDK call,
# no client-lock/hang issues) AND leaves the camera free for direct capture.
# Set RAISE_HEAD=0 to skip. If a daemon is already up, it's reused as-is.
REACHY_VENV="${REACHY_VENV:-.venv}"
if [ "${RAISE_HEAD:-1}" = "1" ] && [ -x "$REACHY_VENV/bin/reachy-mini-daemon" ]; then
  if curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; then
    log "Reusing the daemon already on :${DAEMON_PORT} (head assumed already up)."
  else
    log "Starting Reachy daemon (--no-media) — it wakes the head up and frees the camera..."
    "$REACHY_VENV/bin/reachy-mini-daemon" --no-media >reachy-daemon.log 2>&1 &
    for _ in $(seq 1 40); do curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1 && break; sleep 1; done
    sleep 4  # give the daemon a moment to wake + raise the head
    log "Head raised by the daemon. (Daemon left running so the head stays up.)"
  fi
fi

python -c "import torch;print('GPU:',torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE (CPU only!)')"
log "Running local Cosmos Reason 2 scene description..."
python cosmos_describe.py "$@"
