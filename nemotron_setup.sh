#!/usr/bin/env bash
#
# nemotron_setup.sh — set up + smoke-test the local Nemotron LLM on Ollama.
#
# This is the local-LLM equivalent of cosmos_describe.sh: it makes sure Ollama is
# installed and serving, the Nemotron model is pulled, and the model actually
# responds. No Open WebUI needed — Strands talks to Ollama directly.
#
# Usage:  ./nemotron_setup.sh

set -uo pipefail
cd "$(dirname "$0")"

MODEL="${NEMOTRON_MODEL:-nemotron-3-nano:30b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

log() { printf '\033[1;36m[nemotron]\033[0m %s\n' "$*"; }
ok()  { printf '  \033[1;32m[OK]\033[0m %s\n' "$*"; }
err() { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; }

ollama_up() { curl -sf "${OLLAMA_URL}/api/version" >/dev/null 2>&1; }

# 1) Ollama installed -------------------------------------------------------- #
log "1/4 Ollama binary"
if command -v ollama >/dev/null 2>&1; then
  ok "ollama found: $(command -v ollama)"
else
  log "installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
  command -v ollama >/dev/null 2>&1 || { err "Ollama install failed"; exit 1; }
  ok "Ollama installed"
fi

# 2) Ollama serving ---------------------------------------------------------- #
log "2/4 Ollama service @ ${OLLAMA_URL}"
if ollama_up; then
  ok "serving (v$(curl -sf "${OLLAMA_URL}/api/version" | sed -E 's/.*"version":"([^"]+)".*/\1/'))"
else
  log "starting 'ollama serve' in the background..."
  systemctl start ollama 2>/dev/null || nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
  for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
  ollama_up || { err "Ollama not reachable — see /tmp/ollama-serve.log"; exit 1; }
  ok "serving"
fi

# 3) Model pulled ------------------------------------------------------------ #
log "3/4 Model: ${MODEL}"
if ollama list 2>/dev/null | grep -q "${MODEL%%:*}"; then
  ok "already pulled ($(ollama list | grep "${MODEL%%:*}" | awk '{print $3, $4}'))"
else
  log "pulling ${MODEL} (large, one-time)..."
  ollama pull "${MODEL}" || { err "pull failed"; exit 1; }
  ok "pulled"
fi

# Confirm the model advertises tool-calling (required for the agent).
if ollama show "${MODEL}" 2>/dev/null | grep -qi "tools"; then
  ok "model advertises 'tools' capability (function calling supported)"
else
  err "model does NOT advertise tool support — agent tool use may fail"
fi

# 4) Generation smoke test --------------------------------------------------- #
log "4/4 Generation test (warms the model into GPU)"
t0=$(date +%s)
resp=$(curl -sf "${OLLAMA_URL}/api/generate" -d "{\"model\":\"${MODEL}\",\"prompt\":\"Reply with exactly: nemotron online\",\"stream\":false}" \
  | sed -E 's/.*"response":"([^"]*)".*/\1/')
t1=$(date +%s)
if [ -n "$resp" ]; then
  ok "model responded in $((t1 - t0))s: ${resp}"
else
  err "no response from ${MODEL}"
  exit 1
fi

echo
log "Ready. Ollama @ ${OLLAMA_URL}, model ${MODEL}."
log "Next: ./test_nemotron_agent.sh   (proves Strands + tool calling on Nemotron)"
