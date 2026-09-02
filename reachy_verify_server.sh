#!/usr/bin/env bash
#
# reachy_verify_server.sh — lean client->server preflight for the split deployment.
#
# Run this ON THE CLIENT (robot host) to confirm, before launching the real thing,
# that the GPU box (reachy_server.sh on the Thor, 10.0.0.30) is reachable AND that
# both models actually answer over the network:
#
#   1. Ollama reachable            (GET  /api/version)
#   2. Nemotron present + responds (GET  /api/tags  +  POST /api/generate)
#   3. Cosmos reachable + ready    (GET  /health)
#   4. Camera available + vision   (grab a real frame, POST /look with it)
#
# The camera check tries the local media bus first, then a direct V4L2 capture; if
# neither yields a frame it WARNS and sends a synthetic image so the /look API is
# still exercised (networking green, camera flagged).
#
# Purely a networking + model-API check: it starts no robot, daemon or media bus
# and loads no model locally. Exit 0 = all green, non-zero = something failed.
#
# Usage:  MODEL_SERVER=10.0.0.30 ./reachy_verify_server.sh

set -uo pipefail
cd "$(dirname "$0")"

MODEL_SERVER="${MODEL_SERVER:-10.0.0.30}"
OLLAMA_HOST="${OLLAMA_HOST:-http://${MODEL_SERVER}:11434}"
COSMOS_URL="${COSMOS_URL:-http://${MODEL_SERVER}:8077}"
NEMOTRON_MODEL="${NEMOTRON_MODEL:-nemotron-3-nano:30b}"
REACHY_CAM_SOCK="${REACHY_CAM_SOCK:-/tmp/reachy_cam.sock}"
REACHY_CAMERA="${REACHY_CAMERA:-0}"
export OLLAMA_HOST COSMOS_URL NEMOTRON_MODEL REACHY_CAM_SOCK REACHY_CAMERA

log()  { printf '\033[1;34m[verify]\033[0m %s\n' "$*"; }
pass() { printf '  \033[1;32m[PASS]\033[0m %s\n' "$*"; }
fail() { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; FAILED=1; }
FAILED=0

# Use the client venv if present (gives cv2 / media_bus for a REAL frame); not
# required — the feed test falls back to a synthetic image otherwise.
[ -f ".venv/bin/activate" ] && { . .venv/bin/activate; PY="python"; } || PY="$(command -v python3 || command -v python)"

log "Target model server: ${MODEL_SERVER}"
log "  LLM    -> ${OLLAMA_HOST}   (model ${NEMOTRON_MODEL})"
log "  Vision -> ${COSMOS_URL}"
echo

# ---- 1) Ollama reachable ---------------------------------------------------- #
log "1/4 Ollama reachable  (GET ${OLLAMA_HOST}/api/version)"
ver="$(curl -sf --max-time 5 "${OLLAMA_HOST}/api/version" 2>/dev/null)" \
  && pass "reachable — ${ver}" \
  || fail "cannot reach Ollama at ${OLLAMA_HOST} (is reachy_server.sh up? bound to 0.0.0.0? firewall?)"

# ---- 2) Nemotron present + responds ---------------------------------------- #
log "2/4 Nemotron model  (GET /api/tags + POST /api/generate)"
if curl -sf --max-time 5 "${OLLAMA_HOST}/api/tags" 2>/dev/null | grep -q "${NEMOTRON_MODEL%%:*}"; then
  pass "model '${NEMOTRON_MODEL}' is pulled on the server"
  log "    warming + generating (first call may load the model into GPU)..."
  gen="$(curl -sf --max-time 180 "${OLLAMA_HOST}/api/generate" \
        -d "{\"model\":\"${NEMOTRON_MODEL}\",\"prompt\":\"Reply with exactly: link ok\",\"stream\":false}" 2>/dev/null \
        | $PY -c 'import sys,json;print(json.load(sys.stdin).get("response","").strip())' 2>/dev/null)"
  [ -n "$gen" ] && pass "generate responded: ${gen}" || fail "generate returned nothing"
else
  fail "model '${NEMOTRON_MODEL}' NOT found on the server (run ./reachy_server.sh, or NEMOTRON_MODEL is wrong)"
fi

# ---- 3) Cosmos reachable + ready ------------------------------------------- #
log "3/4 Cosmos reachable  (GET ${COSMOS_URL}/health)"
health="$(curl -sf --max-time 5 "${COSMOS_URL}/health" 2>/dev/null)"
if [ -n "$health" ]; then
  if echo "$health" | grep -q '"ready": *true'; then
    pass "reachable and model loaded — ${health}"
  else
    pass "reachable but still loading — ${health} (retry in a moment)"
  fi
else
  fail "cannot reach Cosmos at ${COSMOS_URL} (is reachy_server.sh up? COSMOS_BIND=0.0.0.0? firewall?)"
fi

# ---- 4) Camera available + vision feed round-trip -------------------------- #
# Check the camera is available (media bus -> direct V4L2), grab ONE real frame,
# base64 it, POST to /look, and print the model's answer. This exercises the full
# client->server vision path including the image payload. No real camera -> WARN
# and fall back to a synthetic frame so the /look API is still verified.
log "4/4 Camera available + vision feed  (POST ${COSMOS_URL}/look with a frame)"
"$PY" - <<'PYEOF'
import base64, json, os, sys, time, urllib.request

COSMOS_URL = os.environ["COSMOS_URL"]
CAM_SOCK   = os.environ.get("REACHY_CAM_SOCK", "/tmp/reachy_cam.sock")
CAMERA     = int(os.environ.get("REACHY_CAMERA", "0"))

def frame_from_bus():
    if not os.path.exists(CAM_SOCK):
        return None, None
    import media_bus
    for _seq, _ts, jpeg in media_bus.subscribe(CAM_SOCK, retries=10, delay=0.1):
        return jpeg, "media-bus"        # first frame is enough
    return None, None

def frame_from_camera():
    import cv2
    cap = cv2.VideoCapture(CAMERA, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None, None
    f = None
    for _ in range(8):                  # skip warm-up empties
        ok, img = cap.read()
        if ok:
            f = img
    cap.release()
    if f is None:
        return None, None
    ok, buf = cv2.imencode(".jpg", f)
    return (buf.tobytes(), "camera") if ok else (None, None)

def frame_synthetic():
    # 2x2 px JPEG so the /look API + payload path is still exercised with no camera.
    b64 = ("/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
           "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAACAAIBAREA/8QAHwAA"
           "AQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
           "BRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RF"
           "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip"
           "qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEB"
           "AAA/APn+iiiiv//Z")
    return base64.b64decode(b64), "synthetic"

# Camera availability: try a REAL frame (media bus first, then direct V4L2).
jpeg, src = (None, None)
for fn in (frame_from_bus, frame_from_camera):
    try:
        jpeg, src = fn()
        if jpeg:
            break
    except Exception as e:
        print(f"    ({fn.__name__} unavailable: {e})")

if jpeg:
    print(f"  \033[1;32m[PASS]\033[0m camera available via {src}  ({len(jpeg)} bytes)")
else:
    print("  \033[1;33m[WARN]\033[0m NO camera available — media bus is down and no /dev/video"
          f"{CAMERA} capture. Vision needs a local camera on the client. "
          "Sending a synthetic frame so the /look API is still verified.")
    jpeg, src = frame_synthetic()
    if not jpeg:
        print("  \033[1;31m[FAIL]\033[0m could not obtain any frame to send"); sys.exit(1)
body = json.dumps({
    "question": "Reply in one short sentence: what do you see?",
    "image": True,
    "image_b64": base64.b64encode(jpeg).decode("ascii"),
}).encode()
req = urllib.request.Request(f"{COSMOS_URL}/look", data=body,
                             headers={"Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        out = json.loads(resp.read())
except Exception as e:
    print(f"  \033[1;31m[FAIL]\033[0m /look request failed: {e}"); sys.exit(1)

ans = (out.get("answer") or "").strip()
if ans:
    print(f"  \033[1;32m[PASS]\033[0m Cosmos answered in {time.time()-t0:.1f}s (mode={out.get('mode')}): {ans}")
else:
    print(f"  \033[1;31m[FAIL]\033[0m /look returned no answer: {out}"); sys.exit(1)
PYEOF
[ $? -eq 0 ] || FAILED=1

echo
if [ "$FAILED" -eq 0 ]; then
  printf '\033[1;32m[verify] All checks passed.\033[0m Client can reach both models on %s. Launch with ./reachy_client.sh\n' "$MODEL_SERVER"
else
  printf '\033[1;31m[verify] One or more checks failed.\033[0m See the [FAIL] lines above.\n' >&2
fi
exit "$FAILED"
