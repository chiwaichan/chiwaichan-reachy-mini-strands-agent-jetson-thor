#!/usr/bin/env bash
#
# test_nemotron_agent.sh — prove Strands can drive the local Nemotron model with
# full tool calling. Reuses the project .venv (where strands lives) and adds the
# 'ollama' client. Run ./nemotron_setup.sh first to ensure the model is serving.
#
# Usage:  ./test_nemotron_agent.sh

set -uo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"
[ -d "$VENV_DIR" ] || { echo "[fatal] $VENV_DIR missing — run ./reachy_assistant.sh once to create it."; exit 1; }
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# OllamaModel needs the 'ollama' python client; strands is already installed.
if command -v uv >/dev/null 2>&1; then
  uv pip install --upgrade strands-agents ollama >/dev/null
else
  pip install --upgrade strands-agents ollama >/dev/null
fi

export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
export NEMOTRON_MODEL="${NEMOTRON_MODEL:-nemotron-3-nano:30b}"

python test_nemotron_agent.py
