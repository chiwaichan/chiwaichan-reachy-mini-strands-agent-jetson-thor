#!/usr/bin/env bash
#
# test_mqtt_look.sh — publish a "look" event to the Reachy MQTT topic.
# Expected: the agent calls look_and_describe (Cosmos Reason 2 vision) to answer
# the visual question, then speaks the answer in one short sentence.
#
# Run ./reachy_assistant.sh in another shell first, then run this.
#
# Usage:
#   ./test_mqtt_look.sh "how many monitors do you see?"
#   ./test_mqtt_look.sh                       # default question below

cd "$(dirname "$0")"
Q="${1:-how many humans can you see?}"

# Build the JSON safely (handles spaces/quotes in the question).
payload=$(Q="$Q" .venv/bin/python -c \
  'import json,os;print(json.dumps({"event":"look","question":os.environ["Q"]}))')

exec ./send_mqtt.sh "$payload"
