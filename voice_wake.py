"""Voice wake-up for the Reachy Mini — say "Hey Reachy" to lift its head.

No LLM. Offline wake-word detection with Vosk (constrained grammar for the exact
phrase) on raw ALSA mic audio; motors driven via the Reachy SDK. The robot sleeps
(head down), and wakes with a little gesture when it hears the phrase, then sleeps
again and keeps listening. Ctrl-C to exit.

Env:
    VOSK_MODEL   Path to a Vosk model dir (voice_wake.sh downloads one).
    MIC_DEV      ALSA capture device (default: auto-detected Reachy card).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils import create_head_pose
from vosk import KaldiRecognizer, Model

# "reachy" is NOT in Vosk's English lexicon, so a constrained grammar would drop
# it and never fire. Use full-vocabulary recognition and match the phonetic
# renderings the model actually emits for "(hey) reachy".
WAKE_TOKENS = ("reachy", "reach", "richie", "ritchie", "reachie", "reaches")


def wake_gesture(mini: ReachyMini) -> None:
    """Lift the head and give a small antenna wiggle to acknowledge the wake."""
    mini.enable_motors()
    mini.goto_target(INIT_HEAD_POSE, antennas=[0.6, 0.6], duration=0.5)
    mini.goto_target(create_head_pose(pitch=-8, degrees=True), antennas=[-0.3, -0.3], duration=0.4)
    mini.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=0.5)


def main() -> int:
    model_path = os.environ.get("VOSK_MODEL", "")
    if not model_path or not os.path.isdir(model_path):
        print(f"[fatal] VOSK_MODEL not set or missing: {model_path!r}")
        return 1
    mic_dev = os.environ.get("MIC_DEV", "plughw:0")

    print("Loading wake-word model...")
    rec = KaldiRecognizer(Model(model_path), 16000)

    print("Connecting to Reachy Mini (motors only)...")
    try:
        connection = ReachyMini(media_backend="no_media", connection_mode="localhost_only")
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] could not connect to daemon: {e}")
        return 1

    with connection as mini:
        try:
            mini.goto_sleep()  # rest head down
        except Exception:  # noqa: BLE001
            pass

        ar = subprocess.Popen(
            ["arecord", "-q", "-D", mic_dev, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE,
        )
        print(f'Asleep. Say "Hey Reachy" to wake it (mic={mic_dev}). Ctrl-C to exit.\n')
        try:
            while True:
                data = ar.stdout.read(4000)
                if not data:
                    print("[warn] mic stream ended (device busy? stop the media daemon).")
                    break
                final = rec.AcceptWaveform(data)
                if final:
                    text = json.loads(rec.Result()).get("text", "")
                    if text:
                        print(f"   (heard: {text})")  # so you can see/tune what it transcribes
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "")
                if text and any(tok in text for tok in WAKE_TOKENS):
                    print(f'>>> heard "{text}" — waking up!')
                    rec.Reset()
                    wake_gesture(mini)
                    time.sleep(3)
                    try:
                        mini.goto_sleep()
                    except Exception:  # noqa: BLE001
                        pass
                    print('Back to sleep. Listening for "Hey Reachy"...\n')
        except KeyboardInterrupt:
            print("\nExiting.")
        finally:
            ar.terminate()
            try:
                ar.wait(timeout=2)
            except Exception:  # noqa: BLE001
                ar.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
