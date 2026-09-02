#!/usr/bin/env bash
#
# test_mqtt_sad.sh — publish a sad/bad-news message to the Reachy MQTT topic.
# Expected: the agent reads the sentiment and plays the 'sad1' move, then speaks
# a short empathetic line.
#
# Run ./reachy_assistant.sh in another shell first, then run this.

cd "$(dirname "$0")"
exec ./send_mqtt.sh '{"message":"I am so sorry, the project was cancelled today"}'
