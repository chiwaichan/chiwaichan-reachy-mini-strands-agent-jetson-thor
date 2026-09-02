#!/usr/bin/env bash
#
# reachy_assistant.sh — "Hey Reachy" -> listen to a request -> answer -> speak.
#
# Idle is pure-local (Vosk wake word, zero LLM). On wake the head raises and the
# spoken request is transcribed offline (still zero LLM); a fresh Strands agent
# (Amazon Nova 2 Lite on Bedrock) then runs ONE capped task and is destroyed. The
# agent picks a tool: local Cosmos Reason 2 vision (.venv-cosmos subprocess) to
# see the room, or query the IoT datalake (AWS S3 Tables / Apache Iceberg, via
# Lambda + Athena).
#
# Usage:  ./reachy_assistant.sh         (say "Hey Reachy"; Ctrl-C to stop)

set -uo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"                 # assistant venv (reachy SDK + vosk + strands + piper)
COSMOS_VENV="${COSMOS_VENV:-.venv-cosmos}"    # heavy CUDA venv for Cosmos (built by cosmos_describe.sh)
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DAEMON_PORT="${DAEMON_PORT:-8000}"
MODEL_DIR="$HOME/.cache/reachy_voice"

# Agent LLM backend: local Nemotron via Ollama (default, $0, offline) or Bedrock.
export LLM_BACKEND="${LLM_BACKEND:-ollama}"        # "ollama" | "bedrock"
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
export NEMOTRON_MODEL="${NEMOTRON_MODEL:-nemotron-3-nano:30b}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.amazon.nova-2-lite-v1:0}"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export MAX_MODEL_CALLS="${MAX_MODEL_CALLS:-12}"   # datalake Q needs discover->schema->query->answer cycles
export LOOK_SECONDS="${LOOK_SECONDS:-4}"

# IoT datalake (queried via Lambda + Athena over AWS S3 Tables / Iceberg).
export DATALAKE_REGION="${DATALAKE_REGION:-us-east-1}"
export DATALAKE_STACK="${DATALAKE_STACK:-iot-datalake}"

# AWS IoT Core MQTT trigger (2nd wake source). WebSocket + SigV4 — reuses AWS
# creds, no certs. Set IOT_TOPIC to enable; unset => voice-only (unchanged).
export IOT_REGION="${IOT_REGION:-us-east-1}"
export IOT_TOPIC="${IOT_TOPIC:-the-project/reachy-mini/XIAOReachyMini/action}"
export IOT_ENDPOINT="${IOT_ENDPOINT:-$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$IOT_REGION" --output text 2>/dev/null)}"
export COSMOS_PY="$(pwd)/$COSMOS_VENV/bin/python"
export VOSK_MODEL="$MODEL_DIR/vosk-model-small-en-us-0.15"

log() { printf '\033[1;36m[assistant]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[assistant]\033[0m %s\n' "$*" >&2; }

# Reachy ALSA card (mic + speaker)
CARD=$(arecord -l 2>/dev/null | awk -F'card |:' '/[Rr]eachy [Mm]ini [Aa]udio|[Rr]e[Ss]peaker/{print $2; exit}')
export MIC_DEV="${MIC_DEV:-plughw:${CARD:-0}}"
export REACHY_AUDIO_CARD="${REACHY_AUDIO_CARD:-${CARD:-0}}"

# Speaker can default to a low ~62% (-23dB); set it loud so the robot is audible.
SPEAKER_VOLUME="${SPEAKER_VOLUME:-85%}"
amixer -c "${CARD:-0}" sset 'PCM',0 "$SPEAKER_VOLUME" >/dev/null 2>&1 || true
amixer -c "${CARD:-0}" sset 'PCM',1 "$SPEAKER_VOLUME" >/dev/null 2>&1 || true
log "Speaker volume set to $SPEAKER_VOLUME (card ${CARD:-0})."

command -v uv >/dev/null 2>&1 || { log "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -d "$VENV_DIR" ] || { log "Creating venv ($VENV_DIR)..."; uv venv "$VENV_DIR" --python "$PYTHON_VERSION"; }
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Installing assistant deps (reachy-mini, vosk, strands-agents, piper-tts)..."
# Pin opencv <5: OpenCV 5.0 removed the Haar cascade API (cv2.CascadeClassifier)
# used by the face-tracker, so the unpinned --upgrade would break reachy_assistant.py.
uv pip install --upgrade reachy-mini vosk strands-agents boto3 awsiotsdk "opencv-python-headless<5" ollama imageio-ffmpeg >/dev/null
uv pip install --upgrade piper-tts >/dev/null 2>&1 || err "piper-tts install failed — will fall back to espeak-ng/print."

# Cosmos venv must exist (vision backend)
[ -x "$COSMOS_PY" ] || { err "Cosmos venv missing ($COSMOS_PY). Run ./cosmos_describe.sh once first."; exit 1; }

# Vosk wake-word model
if [ ! -d "$VOSK_MODEL" ]; then
  log "Downloading Vosk model (one-time)..."; mkdir -p "$MODEL_DIR"
  curl -fsSL -o /tmp/vosk_model.zip "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip" \
    && unzip -q /tmp/vosk_model.zip -d "$MODEL_DIR" && rm -f /tmp/vosk_model.zip || { err "vosk model download failed"; exit 1; }
fi

# Piper voice (offline TTS) — optional; without it, falls back to espeak-ng/print
PIPER_DIR="$MODEL_DIR/piper"; VOICE="$PIPER_DIR/en_US-lessac-medium.onnx"
if python -c "import piper" 2>/dev/null && [ ! -f "$VOICE" ]; then
  log "Downloading Piper voice (one-time, ~60MB)..."; mkdir -p "$PIPER_DIR"
  base="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
  curl -fsSL -o "$VOICE" "$base/en_US-lessac-medium.onnx" \
    && curl -fsSL -o "$VOICE.json" "$base/en_US-lessac-medium.onnx.json" \
    || err "Piper voice download failed — will fall back to espeak-ng/print."
fi
[ -f "$VOICE" ] && export PIPER_VOICE="$VOICE"

# Daemon: --no-media keeps mic + camera free; --no-wake-up-on-start lets the
# controller own the head — it raises the head on startup and keeps it up for
# the whole session (so the head is always up while the assistant runs).
daemon_up() { curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; }
if daemon_up; then
  log "Reusing daemon on :${DAEMON_PORT} (must be --no-media or camera/mic will be busy)."
else
  log "Starting Reachy daemon (--no-media)..."
  reachy-mini-daemon --no-media --no-wake-up-on-start >reachy-daemon.log 2>&1 &
  for _ in $(seq 1 40); do daemon_up && break; sleep 1; done
  daemon_up || { err "daemon not ready — see reachy-daemon.log"; exit 1; }
fi

# Warm Cosmos vision server: loads the VLM once so look_and_describe is fast
# (otherwise each look cold-loads ~5GB). Reuse one if already up; else start it
# and stop it on exit. The assistant falls back to a one-shot subprocess if down.
export COSMOS_PORT="${COSMOS_PORT:-8077}"
# Face tracking owns the camera in-process and shares frames with Cosmos (so the
# head can follow you while the idle/agent vision checks still run). FACE_TRACK=0
# disables it (the Cosmos server then captures the camera itself).
export FACE_TRACK="${FACE_TRACK:-1}"
COSMOS_SERVER_PID=""
MB_CAM_PID=""
MB_AUD_PID=""
cosmos_up() { curl -fsS "http://127.0.0.1:${COSMOS_PORT}/health" >/dev/null 2>&1; }
cleanup() {
  [ -n "$COSMOS_SERVER_PID" ] && kill "$COSMOS_SERVER_PID" 2>/dev/null || true
  [ -n "$MB_CAM_PID" ] && kill "$MB_CAM_PID" 2>/dev/null || true
  [ -n "$MB_AUD_PID" ] && kill "$MB_AUD_PID" 2>/dev/null || true
}
trap cleanup EXIT
if cosmos_up; then
  log "Reusing warm Cosmos server on :${COSMOS_PORT}."
else
  log "Starting warm Cosmos server (loads ~5GB once; first run downloads the model)..."
  "$COSMOS_PY" cosmos_server.py >cosmos-server.log 2>&1 &
  COSMOS_SERVER_PID=$!
  for _ in $(seq 1 600); do cosmos_up && break; sleep 1; done
  cosmos_up || err "Cosmos server not ready yet — see cosmos-server.log (assistant will fall back to subprocess looks)."
fi

# Local LLM: make sure Ollama is serving the Nemotron model (installs/pulls/warms
# via nemotron_setup.sh). Skipped when LLM_BACKEND=bedrock.
if [ "$LLM_BACKEND" = "ollama" ]; then
  log "Agent LLM: local Nemotron — ensuring Ollama + model are ready..."
  ./nemotron_setup.sh || { err "Nemotron not ready (see above). Set LLM_BACKEND=bedrock to use Nova instead."; exit 1; }
fi

# Media bus: one broker owns the camera, one owns the mic, and they fan the live
# streams out over Unix sockets so the assistant's own face-tracker / idle-watcher
# / clip-recorder AND any other process can all consume them at once. Set
# MEDIA_BUS=0 to skip (the assistant then owns the devices in-process, as before).
export MEDIA_BUS="${MEDIA_BUS:-1}"
export REACHY_CAM_SOCK="${REACHY_CAM_SOCK:-/tmp/reachy_cam.sock}"
export REACHY_AUD_SOCK="${REACHY_AUD_SOCK:-/tmp/reachy_audio.sock}"
if [ "$MEDIA_BUS" = "1" ]; then
  log "Starting media bus (camera + mic brokers; fan out to many processes)..."
  python media_bus.py camera >media-cam.log 2>&1 &
  MB_CAM_PID=$!
  python media_bus.py audio  >media-aud.log 2>&1 &
  MB_AUD_PID=$!
  for _ in $(seq 1 50); do [ -S "$REACHY_CAM_SOCK" ] && [ -S "$REACHY_AUD_SOCK" ] && break; sleep 0.1; done
  if [ -S "$REACHY_CAM_SOCK" ] && [ -S "$REACHY_AUD_SOCK" ]; then
    log "Media bus up: camera=$REACHY_CAM_SOCK mic=$REACHY_AUD_SOCK (other processes can subscribe via media_bus.subscribe)."
  else
    err "Media bus failed to start (see media-cam.log / media-aud.log). Assistant will own the devices directly."
    kill "$MB_CAM_PID" "$MB_AUD_PID" 2>/dev/null || true; MB_CAM_PID=""; MB_AUD_PID=""
  fi
fi

if [ "$LLM_BACKEND" = "ollama" ]; then
  log "Ready. Strands Agents orchestrating local Nemotron ($NEMOTRON_MODEL @ $OLLAMA_HOST, \$0, on wake)."
else
  log "Ready. Strands Agents orchestrating Bedrock $BEDROCK_MODEL_ID @ $AWS_REGION (agent only runs on wake)."
fi
log "Datalake: stack $DATALAKE_STACK @ $DATALAKE_REGION (needs AWS creds for CFN + Lambda)."
if [ -n "${IOT_TOPIC:-}" ] && [ -n "${IOT_ENDPOINT:-}" ]; then
  log "MQTT trigger: topic '$IOT_TOPIC' @ $IOT_ENDPOINT ($IOT_REGION). Test with ./send_mqtt.sh"
else
  log "MQTT trigger: disabled (IOT_TOPIC/IOT_ENDPOINT unset) — voice-only."
fi
python reachy_assistant.py
