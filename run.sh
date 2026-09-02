#!/usr/bin/env bash
#
# run.sh — bootstrap + launch the Reachy Mini Lite x Strands agent demo.
#
# It will:
#   1. install uv (fast Python package manager) if missing
#   2. install GStreamer system packages (needed for camera/mic/speaker)
#   3. create a venv and install reachy-mini + strands-agents
#   4. make sure the Reachy Mini daemon is running (starts it if not)
#   5. run agent_demo.py, passing along any instruction you give
#
# Usage:
#   ./run.sh
#   ./run.sh "nod twice then look around the room"
#
# Override behaviour with env vars (see README): AWS_REGION, BEDROCK_MODEL_ID,
# MEDIA_BACKEND, PYTHON_VERSION, DAEMON_PORT, SKIP_SYSTEM_DEPS=1.

set -euo pipefail
cd "$(dirname "$0")"

# ----------------------------- config ------------------------------------- #
VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DAEMON_PORT="${DAEMON_PORT:-8000}"
SKIP_SYSTEM_DEPS="${SKIP_SYSTEM_DEPS:-0}"

export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.amazon.nova-2-lite-v1:0}"
# Motion demo needs no camera/mic/speaker; no_media uses the reliable localhost
# connection. Set MEDIA_BACKEND=default to opt into media (needs WebRTC transport).
export MEDIA_BACKEND="${MEDIA_BACKEND:-no_media}"
# Hard ceiling on Bedrock model calls per run — prevents runaway LLM cost.
export MAX_MODEL_CALLS="${MAX_MODEL_CALLS:-15}"
# GStreamer webrtc Rust plugin (built from gst-plugins-rs) — needed for SDK media.
_GSTRS="$HOME/.local/gst-plugins-rs/lib/aarch64-linux-gnu"
[ -d "$_GSTRS" ] && export GST_PLUGIN_PATH="$_GSTRS:${GST_PLUGIN_PATH:-}"

log() { printf '\033[1;36m[run]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[run]\033[0m %s\n' "$*" >&2; }

# ----------------------------- 1. uv -------------------------------------- #
if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# --------------------- 2. system deps ------------------------------------- #
if [ "$SKIP_SYSTEM_DEPS" != "1" ] && command -v apt-get >/dev/null 2>&1; then
  # Build deps for PyGObject/pycairo — ALWAYS needed: reachy-mini hard-depends on
  # PyGObject on Linux regardless of media backend.
  if ! pkg-config --exists cairo gobject-introspection-1.0 2>/dev/null; then
    log "Installing PyGObject/pycairo build deps (needs sudo)..."
    sudo apt-get update -qq || true
    sudo apt-get install -y --no-install-recommends \
      libcairo2-dev libgirepository1.0-dev gobject-introspection python3-dev pkg-config python3-gi \
      || err "Build-dep install had issues — reachy-mini install may fail."
  fi
  # GStreamer runtime plugins — only when media (camera/mic/speaker) is enabled.
  if [ "$MEDIA_BACKEND" != "no_media" ] && ! pkg-config --exists gstreamer-1.0 2>/dev/null; then
    log "Installing GStreamer runtime (needs sudo)..."
    sudo apt-get install -y --no-install-recommends \
      libgstreamer1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      gstreamer1.0-tools gir1.2-gstreamer-1.0 \
      || err "GStreamer install had issues — re-run with MEDIA_BACKEND=no_media to skip media."
  fi
fi

# Serial-port access: the daemon needs read/write on the Reachy motor board's tty.
# If we can't open it, install the official udev rule (one-time, needs sudo).
if ! find /dev -maxdepth 1 -name 'ttyACM*' -readable -writable 2>/dev/null | grep -q .; then
  if [ -e /etc/udev/rules.d/99-reachy-mini.rules ]; then
    err "Reachy serial port not writable yet — replug the robot, or run: sudo udevadm trigger"
  elif command -v apt-get >/dev/null 2>&1; then
    log "Installing Reachy udev rule for serial access (needs sudo)..."
    sudo tee /etc/udev/rules.d/99-reachy-mini.rules >/dev/null <<'UDEV' || true
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d3", MODE="0666", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="38fb", ATTRS{idProduct}=="1001", MODE="0666", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d3", MODE="0666", GROUP="dialout"
UDEV
    sudo udevadm control --reload-rules && sudo udevadm trigger || true
  fi
fi

# --------------------- 3. venv + python deps ------------------------------ #
if [ ! -d "$VENV_DIR" ]; then
  log "Creating virtualenv ($VENV_DIR) with Python $PYTHON_VERSION..."
  uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

log "Installing Python dependencies (reachy-mini, strands-agents)..."
uv pip install --upgrade reachy-mini strands-agents strands-agents-tools boto3

# --------------------- 4. ensure daemon is running ------------------------ #
daemon_up() { curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; }

DAEMON_PID=""
if daemon_up; then
  log "Reachy Mini daemon already running on :${DAEMON_PORT}."
else
  log "Starting Reachy Mini daemon..."
  reachy-mini-daemon >reachy-daemon.log 2>&1 &
  DAEMON_PID=$!
  for _ in $(seq 1 30); do
    if daemon_up; then log "Daemon is up."; break; fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      err "Daemon exited early — see reachy-daemon.log"; exit 1
    fi
    sleep 1
  done
  if ! daemon_up; then err "Daemon did not become ready — see reachy-daemon.log"; exit 1; fi
fi

cleanup() {
  if [ -n "$DAEMON_PID" ]; then
    log "Stopping daemon we started (pid $DAEMON_PID)..."
    kill "$DAEMON_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --------------------- 5. run the agent ----------------------------------- #
log "Launching Strands agent (Bedrock: $BEDROCK_MODEL_ID @ $AWS_REGION)..."
python agent_demo.py "$@"
