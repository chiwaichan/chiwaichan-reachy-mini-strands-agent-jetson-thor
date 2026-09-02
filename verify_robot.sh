#!/usr/bin/env bash
#
# verify_robot.sh — confirm the Reachy SDK can talk to the connected robot.
#
# Non-destructive: starts the daemon with media off and wake-up off, reads the
# daemon status and live joint feedback through the SDK, then shuts the daemon
# down. NO motors are moved.
#
# Usage:
#   ./verify_robot.sh

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DAEMON_PORT="${DAEMON_PORT:-8000}"

log() { printf '\033[1;36m[verify]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[verify]\033[0m %s\n' "$*" >&2; }

if ! command -v uv >/dev/null 2>&1; then
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d "$VENV_DIR" ]; then
  log "Creating virtualenv ($VENV_DIR) with Python $PYTHON_VERSION..."
  uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

log "Installing Reachy Mini SDK..."
uv pip install --upgrade reachy-mini

daemon_up() { curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; }

DAEMON_PID=""
STARTED_DAEMON=0
if daemon_up; then
  log "A daemon is already running on :${DAEMON_PORT} — using it."
else
  log "Starting daemon (--no-media --no-wake-up-on-start: nothing will move)..."
  reachy-mini-daemon --no-media --no-wake-up-on-start >reachy-daemon.log 2>&1 &
  DAEMON_PID=$!
  STARTED_DAEMON=1
  for _ in $(seq 1 30); do
    if daemon_up; then log "Daemon is up."; break; fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      err "Daemon exited early — last log lines:"; tail -n 20 reachy-daemon.log >&2; exit 1
    fi
    sleep 1
  done
  if ! daemon_up; then err "Daemon did not become ready — see reachy-daemon.log"; exit 1; fi
fi

cleanup() {
  if [ "$STARTED_DAEMON" = "1" ] && [ -n "$DAEMON_PID" ]; then
    log "Stopping daemon we started (pid $DAEMON_PID)..."
    kill "$DAEMON_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

log "Raw daemon status (/api/status):"
curl -fsS "http://localhost:${DAEMON_PORT}/api/status" | python -m json.tool || true
echo

log "Read-only SDK verification:"
python verify_robot.py
