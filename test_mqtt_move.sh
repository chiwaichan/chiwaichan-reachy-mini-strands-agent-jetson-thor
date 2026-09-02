#!/usr/bin/env bash
#
# test_mqtt_move.sh — publish a "move" event to the Reachy MQTT topic.
# Expected: the agent composes a physical gesture with the motion tools
# (nod / shake_head / look_around / wiggle_antennas / spin_body / move_head),
# chaining a few, then speaks one short sentence. Proves motion is a first-class
# MQTT trigger, symmetric with the "look" and "message" events.
#
# Run ./reachy_assistant.sh in another shell first, then run this.
#
# Usage:
#   ./test_mqtt_move.sh "nod twice, then look around the room"
#   ./test_mqtt_move.sh                       # default instruction below

cd "$(dirname "$0")"
INSTR="${1:-nod twice, then wiggle your antennas}"

# Build the JSON safely (handles spaces/quotes in the instruction).
payload=$(INSTR="$INSTR" .venv/bin/python -c \
  'import json,os;print(json.dumps({"event":"move","instruction":os.environ["INSTR"]}))')

exec ./send_mqtt.sh "$payload"
