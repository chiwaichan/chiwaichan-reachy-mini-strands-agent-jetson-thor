#!/usr/bin/env bash
#
# reachy_client.sh — robot-side launcher for the split (client/server) deployment.
#
# Runs on the host the Reachy Mini is plugged into. Drives the robot, camera, mic,
# speaker, wake word (Vosk) and TTS (Piper) locally, but offloads the TWO heavy
# models to the GPU server (reachy_server.sh on the Thor, 10.0.0.30):
#
#   * Agent LLM (Nemotron)      -> Ollama @ http://10.0.0.30:11434
#   * Vision   (Cosmos Reason 2) ->        http://10.0.0.30:8077
#
# So this box needs NO GPU and NO .venv-cosmos. The camera frame is captured here
# and shipped to the vision server as base64 in each look request.
#
# reachy_assistant.sh (the all-in-one single-box launcher) is left untouched — use
# it when the robot and the models share one box.
#
# Usage:  MODEL_SERVER=10.0.0.30 ./reachy_client.sh     (say "Hey Reachy"; Ctrl-C to stop)

set -uo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"                 # light venv — reachy SDK + vosk + strands + piper (NO torch)
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
DAEMON_PORT="${DAEMON_PORT:-8000}"
MODEL_DIR="$HOME/.cache/reachy_voice"

# ---- Model server (the Thor). Override MODEL_SERVER to change the IP. -------- #
MODEL_SERVER="${MODEL_SERVER:-10.0.0.30}"
export LLM_BACKEND="${LLM_BACKEND:-ollama}"                        # "ollama" (remote Thor) | "bedrock" (AWS)
export OLLAMA_HOST="${OLLAMA_HOST:-http://${MODEL_SERVER}:11434}"  # remote Nemotron
export COSMOS_URL="${COSMOS_URL:-http://${MODEL_SERVER}:8077}"     # remote Cosmos vision
export NEMOTRON_MODEL="${NEMOTRON_MODEL:-nemotron-3-nano:30b}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.amazon.nova-2-lite-v1:0}"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export MAX_MODEL_CALLS="${MAX_MODEL_CALLS:-12}"
export LOOK_SECONDS="${LOOK_SECONDS:-4}"

# IoT datalake (Lambda + Athena over S3 Tables / Iceberg) + MQTT trigger — same as
# the single-box launcher; all opt-in and cloud-only.
export DATALAKE_REGION="${DATALAKE_REGION:-us-east-1}"
export DATALAKE_STACK="${DATALAKE_STACK:-iot-datalake}"
export IOT_REGION="${IOT_REGION:-us-east-1}"
export IOT_TOPIC="${IOT_TOPIC:-the-project/reachy-mini/XIAOReachyMini/action}"
export IOT_ENDPOINT="${IOT_ENDPOINT:-$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$IOT_REGION" --output text 2>/dev/null)}"
export VOSK_MODEL="$MODEL_DIR/vosk-model-small-en-us-0.15"

log() { printf '\033[1;36m[client]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[client]\033[0m %s\n' "$*" >&2; }

# Reachy ALSA card (mic + speaker) + speaker volume.
CARD=$(arecord -l 2>/dev/null | awk -F'card |:' '/[Rr]eachy [Mm]ini [Aa]udio|[Rr]e[Ss]peaker/{print $2; exit}')
export MIC_DEV="${MIC_DEV:-plughw:${CARD:-0}}"
export REACHY_AUDIO_CARD="${REACHY_AUDIO_CARD:-${CARD:-0}}"
SPEAKER_VOLUME="${SPEAKER_VOLUME:-85%}"
amixer -c "${CARD:-0}" sset 'PCM',0 "$SPEAKER_VOLUME" >/dev/null 2>&1 || true
amixer -c "${CARD:-0}" sset 'PCM',1 "$SPEAKER_VOLUME" >/dev/null 2>&1 || true
log "Speaker volume set to $SPEAKER_VOLUME (card ${CARD:-0})."

# ---- conda-provided native libs (glib/cairo/gobject-introspection/gstreamer) - #
# reachy-mini pulls pygobject -> pycairo, which build from source and need the
# cairo + gobject-introspection dev files, plus a matching glib + GStreamer
# typelib at runtime. On a box WITHOUT the system -dev packages (e.g. no sudo to
# `apt install libcairo2-dev libgirepository1.0-dev gir1.2-gstreamer-1.0`) we can
# source all of them from an active conda env instead. Guarded on CONDA_PREFIX so
# this is a no-op on the single-box Thor, which uses the system libraries.
#
# One-time conda setup on such a box (no sudo):
#   conda install -n base --override-channels -c conda-forge \
#     gobject-introspection gstreamer gst-plugins-base gst-plugins-good
if [ -n "${CONDA_PREFIX:-}" ] && [ -d "$CONDA_PREFIX/lib/pkgconfig" ]; then
  # build-time: let meson/pkg-config find cairo + gobject-introspection-1.0
  export PKG_CONFIG_PATH="$CONDA_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
  # run-time: pygobject's _gi.so was built against conda glib, so it must load
  # conda's glib (not the older system one) or you get an undefined-symbol error.
  export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
  # run-time: where gi.require_version('Gst','1.0') finds the GStreamer typelib.
  export GI_TYPELIB_PATH="$CONDA_PREFIX/lib/girepository-1.0:${GI_TYPELIB_PATH:-}"
  # gst pulls conda's alsa-lib onto LD_LIBRARY_PATH, so system arecord/aplay now
  # load conda's libasound — which hunts for ALSA plugins under $CONDA_PREFIX and
  # can't find the pulse module, killing mic capture. Point it back at the system
  # plugin dir so audio (mic + Piper playback) keeps working.
  if [ -z "${ALSA_PLUGIN_DIR:-}" ]; then
    _sys_alsa=$(dirname "$(ls /usr/lib/*/alsa-lib/libasound_module_conf_pulse.so 2>/dev/null | head -1)" 2>/dev/null)
    [ -d "$_sys_alsa" ] && export ALSA_PLUGIN_DIR="$_sys_alsa"
  fi
  log "Using conda native libs from $CONDA_PREFIX (glib/cairo/gobject-introspection/gstreamer)${ALSA_PLUGIN_DIR:+; ALSA plugins=$ALSA_PLUGIN_DIR}."
fi

# ---- light venv + deps (NO .venv-cosmos, NO torch/transformers) ------------- #
command -v uv >/dev/null 2>&1 || { log "Installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
[ -d "$VENV_DIR" ] || { log "Creating venv ($VENV_DIR)..."; uv venv "$VENV_DIR" --python "$PYTHON_VERSION"; }
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Installing client deps (reachy-mini, vosk, strands-agents, piper-tts)..."
# Pin opencv <5: OpenCV 5.0 dropped the Haar cascade API the face-tracker uses.
uv pip install --upgrade reachy-mini vosk strands-agents boto3 awsiotsdk "opencv-python-headless<5" ollama imageio-ffmpeg >/dev/null
uv pip install --upgrade piper-tts >/dev/null 2>&1 || err "piper-tts install failed — will fall back to espeak-ng/print."

# Fail loudly here if reachy-mini didn't actually install — otherwise the only
# symptom is a confusing "daemon not ready" much further down. The usual cause is
# a source build of pygobject/pycairo failing for lack of the cairo /
# gobject-introspection dev files (see the conda block above, or apt-install
# libcairo2-dev libgirepository1.0-dev gir1.2-gstreamer-1.0).
if ! command -v reachy-mini-daemon >/dev/null 2>&1; then
  err "reachy-mini-daemon not installed — the 'uv pip install' above failed (likely a pygobject/pycairo build error)."
  err "Re-run the install WITHOUT '>/dev/null' to see the real error:"
  err "  source $VENV_DIR/bin/activate && uv pip install --upgrade reachy-mini"
  err "If it says cairo/gobject-introspection not found, install the native libs (conda block at top of this script, or 'sudo apt install libcairo2-dev libgirepository1.0-dev gir1.2-gstreamer-1.0')."
  exit 1
fi

# Vosk wake-word model (offline STT).
if [ ! -d "$VOSK_MODEL" ]; then
  log "Downloading Vosk model (one-time)..."; mkdir -p "$MODEL_DIR"
  curl -fsSL -o /tmp/vosk_model.zip "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip" \
    && unzip -q /tmp/vosk_model.zip -d "$MODEL_DIR" && rm -f /tmp/vosk_model.zip || { err "vosk model download failed"; exit 1; }
fi

# Piper voice (offline TTS) — optional; without it, falls back to espeak-ng/print.
PIPER_DIR="$MODEL_DIR/piper"; VOICE="$PIPER_DIR/en_US-lessac-medium.onnx"
if python -c "import piper" 2>/dev/null && [ ! -f "$VOICE" ]; then
  log "Downloading Piper voice (one-time, ~60MB)..."; mkdir -p "$PIPER_DIR"
  base="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
  curl -fsSL -o "$VOICE" "$base/en_US-lessac-medium.onnx" \
    && curl -fsSL -o "$VOICE.json" "$base/en_US-lessac-medium.onnx.json" \
    || err "Piper voice download failed — will fall back to espeak-ng/print."
fi
[ -f "$VOICE" ] && export PIPER_VOICE="$VOICE"

# ---- preflight: the two remote models must be reachable --------------------- #
# (Deep verification — including a live camera-frame round-trip — is in
#  reachy_verify_server.sh; this is just a fast go/no-go before we start.)
if [ "$LLM_BACKEND" = "ollama" ]; then
  if curl -sf "${OLLAMA_HOST}/api/version" >/dev/null 2>&1; then
    log "Remote LLM reachable: Ollama @ ${OLLAMA_HOST} (model ${NEMOTRON_MODEL})."
  else
    err "Remote Ollama NOT reachable at ${OLLAMA_HOST}. Start ./reachy_server.sh on ${MODEL_SERVER} (or LLM_BACKEND=bedrock)."
  fi
fi
if curl -sf "${COSMOS_URL}/health" >/dev/null 2>&1; then
  log "Remote vision reachable: Cosmos @ ${COSMOS_URL}."
else
  err "Remote Cosmos NOT reachable at ${COSMOS_URL}. Start ./reachy_server.sh on ${MODEL_SERVER}. (This client has no local GPU fallback — looks will fail.)"
fi

# ---- Reachy daemon (--no-media so the media bus can own camera + mic) ------- #
daemon_up() { curl -fsS "http://localhost:${DAEMON_PORT}/docs" >/dev/null 2>&1; }
if daemon_up; then
  log "Reusing daemon on :${DAEMON_PORT} (must be --no-media or camera/mic will be busy)."
else
  log "Starting Reachy daemon (--no-media)..."
  reachy-mini-daemon --no-media --no-wake-up-on-start >reachy-daemon.log 2>&1 &
  for _ in $(seq 1 40); do daemon_up && break; sleep 1; done
  daemon_up || { err "daemon not ready — see reachy-daemon.log"; exit 1; }
fi

# ---- media bus: camera + mic brokers fan out over Unix sockets -------------- #
# The face-tracker keeps _latest_frame fresh; look_and_describe ships that frame
# to the REMOTE Cosmos server as base64 (so the server never needs a camera).
export FACE_TRACK="${FACE_TRACK:-1}"
export MEDIA_BUS="${MEDIA_BUS:-1}"
export REACHY_CAM_SOCK="${REACHY_CAM_SOCK:-/tmp/reachy_cam.sock}"
export REACHY_AUD_SOCK="${REACHY_AUD_SOCK:-/tmp/reachy_audio.sock}"
MB_CAM_PID=""; MB_AUD_PID=""
cleanup() {
  [ -n "$MB_CAM_PID" ] && kill "$MB_CAM_PID" 2>/dev/null || true
  [ -n "$MB_AUD_PID" ] && kill "$MB_AUD_PID" 2>/dev/null || true
}
trap cleanup EXIT
if [ "$MEDIA_BUS" = "1" ]; then
  log "Starting media bus (camera + mic brokers; fan out to many processes)..."
  python media_bus.py camera >media-cam.log 2>&1 &
  MB_CAM_PID=$!
  python media_bus.py audio  >media-aud.log 2>&1 &
  MB_AUD_PID=$!
  for _ in $(seq 1 50); do [ -S "$REACHY_CAM_SOCK" ] && [ -S "$REACHY_AUD_SOCK" ] && break; sleep 0.1; done
  if [ -S "$REACHY_CAM_SOCK" ] && [ -S "$REACHY_AUD_SOCK" ]; then
    log "Media bus up: camera=$REACHY_CAM_SOCK mic=$REACHY_AUD_SOCK."
  else
    err "Media bus failed to start (see media-cam.log / media-aud.log). Assistant will own the devices directly."
    kill "$MB_CAM_PID" "$MB_AUD_PID" 2>/dev/null || true; MB_CAM_PID=""; MB_AUD_PID=""
  fi
fi

if [ "$LLM_BACKEND" = "ollama" ]; then
  log "Ready. Robot local; agent brain = remote Nemotron @ ${OLLAMA_HOST}, vision = remote Cosmos @ ${COSMOS_URL}."
else
  log "Ready. Robot local; agent brain = Bedrock ${BEDROCK_MODEL_ID} @ ${AWS_REGION}; vision = remote Cosmos @ ${COSMOS_URL}."
fi
if [ -n "${IOT_TOPIC:-}" ] && [ -n "${IOT_ENDPOINT:-}" ]; then
  log "MQTT trigger: topic '$IOT_TOPIC' @ $IOT_ENDPOINT ($IOT_REGION). Test with ./send_mqtt.sh"
else
  log "MQTT trigger: disabled (IOT_TOPIC/IOT_ENDPOINT unset) — voice-only."
fi
python reachy_assistant.py
