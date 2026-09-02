#!/usr/bin/env bash
#
# send_mqtt.sh — publish a test MQTT message to the Reachy IoT topic.
#
# Uses the AWS CLI (default creds, SigV4 — same creds the assistant uses) to
# publish to AWS IoT Core, which is the second trigger source the assistant
# subscribes to. Handy for exercising the MQTT path without a real IoT device.
#
# Usage:
#   ./send_mqtt.sh '{"event":"water_leak","room":"kitchen"}'
#   ./send_mqtt.sh                          # sends the default sample payload
#   IOT_TOPIC=the-project/reachy-mini/XIAOReachyMini/action ./send_mqtt.sh '{"event":"door_open"}'

set -uo pipefail
cd "$(dirname "$0")"

# Keep these defaults in lock-step with reachy_assistant.sh.
IOT_TOPIC="${IOT_TOPIC:-the-project/reachy-mini/XIAOReachyMini/action}"
IOT_REGION="${IOT_REGION:-us-east-1}"
IOT_ENDPOINT="${IOT_ENDPOINT:-}"          # optional; auto-resolved when empty

DEFAULT_PAYLOAD='{"event":"hello","message":"Hello from an MQTT test"}'
PAYLOAD="${1:-$DEFAULT_PAYLOAD}"

log() { printf '\033[1;36m[send-mqtt]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[send-mqtt]\033[0m %s\n' "$*" >&2; }

command -v aws >/dev/null 2>&1 || { err "aws CLI not found (needed to publish)."; exit 1; }

# Resolve the account's ATS data endpoint if not provided explicitly.
if [ -z "$IOT_ENDPOINT" ]; then
  IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --region "$IOT_REGION" --output text 2>/dev/null)
  [ -n "$IOT_ENDPOINT" ] || { err "could not resolve IoT endpoint — check AWS creds/region."; exit 1; }
fi

log "topic    : $IOT_TOPIC"
log "region   : $IOT_REGION"
log "endpoint : $IOT_ENDPOINT"
log "payload  : $PAYLOAD"

if aws iot-data publish \
  --topic "$IOT_TOPIC" \
  --region "$IOT_REGION" \
  --endpoint-url "https://$IOT_ENDPOINT" \
  --cli-binary-format raw-in-base64-out \
  --payload "$PAYLOAD"; then
  log "sent."
else
  err "publish failed."
  exit 1
fi
