#!/usr/bin/env bash
#
# test_cosmos_look.sh — ask the warm Cosmos vision server a question and print
# the answer. Uses the robot camera (V4L2). Requires the server running:
#   ./reachy_assistant.sh                     (starts it on :8077), or standalone:
#   .venv-cosmos/bin/python cosmos_server.py
#
# Usage:
#   ./test_cosmos_look.sh "what color is the mug?"
#   ./test_cosmos_look.sh                      # default: "what do you see?"

set -uo pipefail
cd "$(dirname "$0")"

COSMOS_PORT="${COSMOS_PORT:-8077}"
LOOK_S="${LOOK_SECONDS:-4}"
Q="${1:-what do you see?}"

log() { printf '\033[1;36m[cosmos-look]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[cosmos-look]\033[0m %s\n' "$*" >&2; }

if ! curl -fsS "http://127.0.0.1:${COSMOS_PORT}/health" >/dev/null 2>&1; then
  err "Cosmos server not up on :${COSMOS_PORT}. Start ./reachy_assistant.sh or cosmos_server.py."
  exit 1
fi

# Build the JSON body safely (handles spaces/quotes in the question).
payload=$(Q="$Q" LOOK_S="$LOOK_S" .venv/bin/python -c \
  'import json,os;print(json.dumps({"question":os.environ["Q"],"seconds":float(os.environ["LOOK_S"])}))')

log "asking: $Q"
curl -s -X POST "http://127.0.0.1:${COSMOS_PORT}/look" \
  -H 'Content-Type: application/json' -d "$payload"
echo
