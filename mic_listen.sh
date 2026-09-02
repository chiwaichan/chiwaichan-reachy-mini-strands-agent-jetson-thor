#!/usr/bin/env bash
#
# mic_listen.sh — standalone live mic listener for the Reachy Mini.
# No Claude session, no venv, no numpy. Captures from the Reachy audio card and
# prints a real-time level meter, flagging when it hears sound above a threshold.
#
# Usage:
#   ./mic_listen.sh                 # auto-detect Reachy card, default threshold
#   THRESH=0.003 ./mic_listen.sh    # more/less sensitive (RMS 0..1)
#   MIC_DEV=plughw:0 ./mic_listen.sh
#
# Speak toward the robot; watch for ">>> HEARD". Ctrl-C to stop.

set -uo pipefail

CARD=$(arecord -l 2>/dev/null | awk -F'card |:' '/[Rr]eachy [Mm]ini [Aa]udio|[Rr]e[Ss]peaker/{print $2; exit}')
CARD=${CARD:-0}
export MIC_DEV="${MIC_DEV:-plughw:${CARD}}"
export THRESH="${THRESH:-0.005}"
export RATE=16000
export CH=2

echo "=== Reachy mic listener ==="
echo "  device   : $MIC_DEV (card $CARD)"
echo "  threshold: $THRESH  (RMS, 0..1; lower = more sensitive)"
echo "  Speak toward the robot. Ctrl-C to stop."

if curl -fsS http://localhost:8000/docs >/dev/null 2>&1; then
  echo "  [warn] daemon running on :8000 — if you get 'Device or resource busy',"
  echo "         stop it first (it holds the mic), then re-run."
fi
echo

arecord -D "$MIC_DEV" -f S16_LE -r "$RATE" -c "$CH" -t raw 2>/tmp/mic_listen.err | python3 -u -c '
import sys, os, time, struct, math

rate=int(os.environ["RATE"]); ch=int(os.environ["CH"]); thresh=float(os.environ["THRESH"])
frames=int(rate*0.1); nbytes=frames*ch*2          # 0.1s chunks
stdin=sys.stdin.buffer

def readn(n):
    b=b""
    while len(b)<n:
        c=stdin.read(n-len(b))
        if not c: return b
        b+=c
    return b

print("listening...\n", flush=True)
peak_overall=0.0; detections=0; heard_prev=False
while True:
    data=readn(nbytes)
    if len(data)<nbytes: break
    s=struct.unpack("<%dh"%(len(data)//2), data)
    sq=sum(v*v for v in s); pk=max(abs(v) for v in s)
    rms=math.sqrt(sq/len(s))/32768.0; pk/=32768.0
    peak_overall=max(peak_overall, pk)
    heard = rms>=thresh
    bars=int(min(rms/(thresh*2),1.0)*30); meter="#"*bars+"-"*(30-bars)
    if heard and not heard_prev:
        detections+=1
        sys.stdout.write("\r>>> HEARD sound  RMS=%.4f peak=%.4f  (detection #%d at %s)\n"%(
            rms,pk,detections,time.strftime("%H:%M:%S")))
    heard_prev=heard
    sys.stdout.write("\rlevel |%s| RMS=%.5f peak=%.5f  max-peak=%.4f  detections=%d   "%(
        meter,rms,pk,peak_overall,detections))
    sys.stdout.flush()
'
ec=${PIPESTATUS[0]}
echo
# 130=SIGINT, 141=SIGPIPE: normal when you Ctrl-C — not an error.
if [ "$ec" != "0" ] && [ "$ec" != "130" ] && [ "$ec" != "141" ]; then
  echo "[error] arecord exited ($ec). Last error:"; tail -2 /tmp/mic_listen.err 2>/dev/null
fi
