#!/usr/bin/env bash
#
# voice_wake.sh — say "Hey Reachy" to make the robot lift its head.
# Standalone, no LLM. Offline wake word (Vosk) on the raw mic; motors via SDK.
#
# Usage:  ./voice_wake.sh
# Speak "Hey Reachy". Ctrl-C to stop.

set -uo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DAEMON_PORT="${DAEMON_PORT:-8000}"
MODEL_DIR="$HOME/.cache/reachy_voice"
MODEL_NAME="vosk-model-small-en-us-0.15"
export VOSK_MODEL="$MODEL_DIR/$MODEL_NAME"

# Auto-detect the Reachy ALSA capture card.
CARD=$(arecord -l 2>/dev/null | awk -F'card |:' '/[Rr]eachy [Mm]ini [Aa]udio|[Rr]e[Ss]peaker/{print $2; exit}')
export MIC_DEV="${MIC_DEV:-plughw:${CARD:-0}}"

log() { printf '\033[1;36m[wake]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[wake]\033[0m %s\n' "$*" >&2; }

command -v uv >/dev/null 2>&1 || { log "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -d "$VENV_DIR" ] || { log "Creating venv..."; uv venv "$VENV_DIR" --python "$PYTHON_VERSION"; }
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Installing deps (reachy-mini, vosk)..."
uv pip install --upgrade reachy-mini vosk >/dev/null

# Download the offline wake-word model once.
if [ ! -d "$VOSK_MODEL" ]; then
  log "Downloading Vosk model ($MODEL_NAME, ~40MB, one-time)..."
  mkdir -p "$MODEL_DIR"
  curl -fsSL -o /tmp/vosk_model.zip "https://alphacephei.com/vosk/models/${MODEL_NAME}.zip" \
    || { err "model download failed"; exit 1; }
  unzip -q /tmp/vosk_model.zip -d "$MODEL_DIR" && rm -f /tmp/vosk_model.zip
fi
log "Model: $VOSK_MODEL"

# Need a daemon WITHOUT media so the mic is free for raw ALSA capture.
daemon_up() { curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; }
DAEMON_PID=""; STARTED=0
if daemon_up; then
  log "Daemon already on :${DAEMON_PORT} — if the mic is busy, stop it (it may hold the mic with media on)."
else
  log "Starting daemon (--no-media so the mic is free)..."
  reachy-mini-daemon --no-media --no-wake-up-on-start >reachy-daemon.log 2>&1 &
  DAEMON_PID=$!; STARTED=1
  for _ in $(seq 1 30); do daemon_up && break; kill -0 "$DAEMON_PID" 2>/dev/null || { err "daemon exited:"; tail -n15 reachy-daemon.log >&2; exit 1; }; sleep 1; done
  daemon_up || { err "daemon not ready"; exit 1; }
fi
cleanup() { if [ "$STARTED" = "1" ] && [ -n "$DAEMON_PID" ]; then log "Stopping daemon..."; kill "$DAEMON_PID" 2>/dev/null || true; fi; }
trap cleanup EXIT

python voice_wake.py
