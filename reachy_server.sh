#!/usr/bin/env bash
#
# reachy_server.sh — model server for the split (client/server) deployment.
#
# Runs on the GPU box (the Jetson Thor, 10.0.0.30) and serves the TWO heavy models
# over the LAN so a light, GPU-less client (reachy_client.sh on the robot host) can
# drive the robot without loading any model locally:
#
#   * Nemotron LLM        -> Ollama,           bound to 0.0.0.0:11434
#   * Cosmos Reason 2 VLM  -> cosmos_server.py, bound to 0.0.0.0:8077
#
# No robot, daemon, camera or mic here — this box only answers model calls. The
# camera FRAME travels in from the client as base64 in each /look request, so the
# Cosmos server never touches a local camera.
#
# reachy_assistant.sh (the all-in-one single-box launcher) is left untouched — use
# it when the robot and the models share one box.
#
# Usage:  ./reachy_server.sh        (Ctrl-C to stop the Cosmos server)

set -uo pipefail
cd "$(dirname "$0")"

COSMOS_VENV="${COSMOS_VENV:-.venv-cosmos}"       # heavy CUDA venv (built by ./cosmos_describe.sh)
export COSMOS_PY="$(pwd)/$COSMOS_VENV/bin/python"

# Bind both model servers to all interfaces so LAN clients can reach them.
BIND_ADDR="${BIND_ADDR:-0.0.0.0}"
export COSMOS_PORT="${COSMOS_PORT:-8077}"
export COSMOS_BIND="${COSMOS_BIND:-$BIND_ADDR}"          # cosmos_server.py honors this
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
export NEMOTRON_MODEL="${NEMOTRON_MODEL:-nemotron-3-nano:30b}"

log() { printf '\033[1;35m[server]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[server]\033[0m %s\n' "$*" >&2; }

# Cosmos venv must exist (build it once with ./cosmos_describe.sh).
[ -x "$COSMOS_PY" ] || { err "Cosmos venv missing ($COSMOS_PY). Run ./cosmos_describe.sh once first."; exit 1; }

COSMOS_SERVER_PID=""
cleanup() { [ -n "$COSMOS_SERVER_PID" ] && kill "$COSMOS_SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

# ---- 1) Ollama (Nemotron) on ${BIND_ADDR}:${OLLAMA_PORT} -------------------- #
# Start 'ollama serve' bound to all interfaces BEFORE nemotron_setup.sh, so a fresh
# server is LAN-reachable (nemotron_setup.sh then only pulls / warms / tool-tests,
# talking to it over loopback). We do NOT export OLLAMA_HOST globally, so the ollama
# CLI keeps using its 127.0.0.1 default for the pull/warm calls.
ollama_up() { curl -sf "http://127.0.0.1:${OLLAMA_PORT}/api/version" >/dev/null 2>&1; }
if ollama_up; then
  log "Ollama already serving on :${OLLAMA_PORT}."
  log "  NOTE: if it was started on 127.0.0.1 only, LAN clients can't reach it — restart it with OLLAMA_HOST=${BIND_ADDR}:${OLLAMA_PORT}."
else
  command -v ollama >/dev/null 2>&1 || { log "Installing Ollama..."; curl -fsSL https://ollama.com/install.sh | sh; }
  log "Starting 'ollama serve' bound to ${BIND_ADDR}:${OLLAMA_PORT}..."
  OLLAMA_HOST="${BIND_ADDR}:${OLLAMA_PORT}" nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
  for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
  ollama_up || { err "Ollama not reachable — see /tmp/ollama-serve.log"; exit 1; }
fi
# Pull + warm + tool-capability check (reuses the existing helper, over loopback).
log "Ensuring Nemotron model '${NEMOTRON_MODEL}' is pulled and warm..."
OLLAMA_URL="http://127.0.0.1:${OLLAMA_PORT}" ./nemotron_setup.sh \
  || { err "Nemotron model not ready — see above."; exit 1; }

# ---- 2) Cosmos Reason 2 VLM on ${BIND_ADDR}:${COSMOS_PORT} ------------------ #
cosmos_up() { curl -fsS "http://127.0.0.1:${COSMOS_PORT}/health" >/dev/null 2>&1; }
if cosmos_up; then
  log "Reusing warm Cosmos server on :${COSMOS_PORT}."
else
  log "Starting warm Cosmos server on ${COSMOS_BIND}:${COSMOS_PORT} (loads ~5GB once; first run downloads the model)..."
  "$COSMOS_PY" cosmos_server.py >cosmos-server.log 2>&1 &
  COSMOS_SERVER_PID=$!
  for _ in $(seq 1 600); do cosmos_up && break; sleep 1; done
  cosmos_up || { err "Cosmos server not ready — see cosmos-server.log"; exit 1; }
fi

MY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "Model server ready."
log "  LLM    (Ollama / Nemotron) -> http://${BIND_ADDR}:${OLLAMA_PORT}   model=${NEMOTRON_MODEL}"
log "  Vision (Cosmos Reason 2)   -> http://${BIND_ADDR}:${COSMOS_PORT}"
log "Point the client at this box (${MY_IP:-10.0.0.30}):"
log "  MODEL_SERVER=${MY_IP:-10.0.0.30} ./reachy_client.sh"
log "Serving. Ctrl-C to stop."

# Stay in the foreground so the trap stops the Cosmos server on Ctrl-C. Ollama
# keeps running as its own background process (or systemd unit).
if [ -n "$COSMOS_SERVER_PID" ]; then
  wait "$COSMOS_SERVER_PID"
else
  while true; do sleep 3600; done   # Cosmos was reused, not ours — idle until interrupted
fi
