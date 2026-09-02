#!/usr/bin/env bash
#
# test_mqtt_success.sh — publish a celebratory message to the Reachy MQTT topic.
# Expected: the agent reads the sentiment and plays the 'success1' move, then
# speaks a short congratulatory line.
#
# Run ./reachy_assistant.sh in another shell first, then run this.

cd "$(dirname "$0")"
exec ./send_mqtt.sh '{"message":"great job team, we hit the target!"}'
