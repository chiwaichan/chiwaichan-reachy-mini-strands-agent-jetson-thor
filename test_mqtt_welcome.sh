#!/usr/bin/env bash
#
# test_mqtt_welcome.sh — publish a friendly greeting to the Reachy MQTT topic.
# Expected: the agent reads the sentiment and plays the 'welcoming1' move, then
# speaks a short welcoming line.
#
# Run ./reachy_assistant.sh in another shell first, then run this.

cd "$(dirname "$0")"
exec ./send_mqtt.sh '{"message":"hi there, welcome home!"}'
