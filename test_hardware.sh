#!/usr/bin/env bash
#
# test_hardware.sh — pure-SDK hardware self-test for the Reachy Mini Lite.
# NO LLM / Strands. Exercises every hardware feature and confirms each one.
#
# Starts a daemon WITH media (camera/mic/speaker), runs hardware_check.py,
# then shuts the daemon down. The robot WILL move during this test.
#
# Usage:  ./test_hardware.sh

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DAEMON_PORT="${DAEMON_PORT:-8000}"

# GStreamer webrtc Rust plugin (built from gst-plugins-rs) — required for media.
_GSTRS="$HOME/.local/gst-plugins-rs/lib/aarch64-linux-gnu"
[ -d "$_GSTRS" ] && export GST_PLUGIN_PATH="$_GSTRS:${GST_PLUGIN_PATH:-}"

log() { printf '\033[1;36m[hw-test]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[hw-test]\033[0m %s\n' "$*" >&2; }

if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"
fi
if [ ! -d "$VENV_DIR" ]; then
  log "Creating virtualenv ($VENV_DIR)..."; uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Installing Reachy Mini SDK..."
uv pip install --upgrade reachy-mini >/dev/null

# Verify the media plugin is actually loadable before we depend on it.
if ! gst-inspect-1.0 webrtcsink >/dev/null 2>&1; then
  err "webrtcsink GStreamer plugin not found (GST_PLUGIN_PATH=$GST_PLUGIN_PATH)."
  err "Build it per the README media setup, then re-run."
  exit 1
fi
log "webrtcsink plugin: OK"

daemon_up() { curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; }
DAEMON_PID=""; STARTED=0
rm -f /tmp/reachymini_camera_socket
if daemon_up; then
  log "Daemon already running on :${DAEMON_PORT} — using it."
else
  log "Starting daemon WITH media..."
  reachy-mini-daemon >reachy-daemon.log 2>&1 &
  DAEMON_PID=$!; STARTED=1
  for _ in $(seq 1 30); do
    daemon_up && break
    kill -0 "$DAEMON_PID" 2>/dev/null || { err "Daemon exited early:"; tail -n 20 reachy-daemon.log >&2; exit 1; }
    sleep 1
  done
  daemon_up || { err "Daemon not ready — see reachy-daemon.log"; exit 1; }
fi

# Wait for the camera IPC socket so the SDK picks the LOCAL backend.
for _ in $(seq 1 15); do [ -S /tmp/reachymini_camera_socket ] && break; sleep 1; done
[ -S /tmp/reachymini_camera_socket ] && log "Camera IPC socket present (LOCAL media ready)." \
  || err "Camera IPC socket missing — camera/mic/speaker tests may fall back/fail."

cleanup() { if [ "$STARTED" = "1" ] && [ -n "$DAEMON_PID" ]; then log "Stopping daemon (pid $DAEMON_PID)..."; kill "$DAEMON_PID" 2>/dev/null || true; fi; }
trap cleanup EXIT

log "Running hardware self-test (robot will move)..."
python hardware_check.py
