#!/usr/bin/env bash
#
# test_bedrock.sh — verify Amazon Bedrock access for the Strands agent.
#
# No robot needed. Confirms (1) AWS credentials resolve via the default profile
# and (2) a Strands agent can complete one request against the Bedrock model.
# Run this before run.sh to isolate Bedrock/AWS problems from robot problems.
#
# Usage:
#   ./test_bedrock.sh
#
# Override with env vars: AWS_REGION, BEDROCK_MODEL_ID, PYTHON_VERSION.

set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
# Amazon Nova 2 Lite on Bedrock via US cross-region inference profile.
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.amazon.nova-2-lite-v1:0}"

log() { printf '\033[1;36m[bedrock-test]\033[0m %s\n' "$*"; }

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

log "Installing Bedrock test dependencies (strands-agents, boto3)..."
uv pip install --upgrade strands-agents boto3

log "Testing Bedrock: $BEDROCK_MODEL_ID @ $AWS_REGION"
python test_bedrock.py
