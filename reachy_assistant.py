"""Reachy voice assistant: "Hey Reachy" -> listen to a request -> answer -> speak.

Cost-minimal lifecycle:
  - IDLE: only an offline Vosk wake-word listener runs. NO Strands agent, NO
    Bedrock — zero LLM cost while waiting.
  - ON WAKE: raise the head (the "I'm listening" cue), then transcribe ONE spoken
    request with the same offline Vosk model (still zero LLM cost). Instantiate a
    FRESH Strands agent (Amazon Nova 2 Lite on Bedrock) with that request as its
    task, run it under a hard call cap. The agent picks a tool: look at the room
    (local Cosmos Reason 2 vision) or query the IoT datalake (AWS S3 Tables /
    Apache Iceberg, via Lambda + Athena). Speak the short reply via local TTS,
    print a token/latency/cost summary, then DESTROY the agent and rest. Back to
    idle.

A SECOND trigger source runs alongside the wake word: an AWS IoT Core MQTT
subscription (WebSocket + SigV4, reusing the AWS creds). A message on the
configured topic is turned into a request and fed to the SAME fresh-agent task.
Both sources enqueue to a single worker that owns the robot, so the motors and
the agent are never driven by two sources at once. No IOT_TOPIC set => the MQTT
listener stays off and behaviour is voice-only, identical to before.

The SAME MQTT connection is also used to UPLOAD telemetry back to AWS IoT: after
each agent action (emotion played, scene described, presence seen, final spoken
reply) a full robot-state snapshot is published to a state topic ("reachy-mini/
state" by default). Each wake interaction is also recorded — frames are sampled
from the shared camera buffer for the duration, encoded to MP4, uploaded to S3,
and a presigned download URL is included in the final "reply" message. Both are
non-blocking and a no-op when MQTT/camera are off, so a voice-only run is
unaffected.

The heavy Cosmos Reason 2 model runs in the separate .venv-cosmos via subprocess.
One --no-media daemon gives motors + a free mic + a free camera.
"""

from __future__ import annotations

import gc
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import media_bus  # one-owner-per-device camera/mic fan-out (see media_bus.py)
from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils import create_head_pose
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands.session import FileSessionManager
from vosk import KaldiRecognizer, Model

# ---- config (env-overridable) --------------------------------------------- #
WAKE_TOKENS = ("reachy", "reach", "richie", "ritchie", "reachie")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_MODEL_CALLS = int(os.environ.get("MAX_MODEL_CALLS", "12"))  # datalake Q = discover->schema->query->answer

# LLM backend for the agent: local Nemotron via Ollama (default, $0, offline) or
# Amazon Nova on Bedrock. The tools/system prompt are identical either way.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama").lower()   # "ollama" | "bedrock"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nemotron-3-nano:30b")
ACTIVE_MODEL = NEMOTRON_MODEL if LLM_BACKEND == "ollama" else MODEL_ID

# Conversational memory (feature #4): persist each wake's messages to disk so the
# fresh per-wake agent can recall recent turns ("...and what about yesterday?").
# Local JSON via Strands FileSessionManager — $0, no cloud, no LLM while idle (it
# is just message storage). The session id rotates after SESSION_TTL seconds of
# inactivity, starting a "new conversation". SESSION_MEMORY=0 restores the old
# stateless-per-wake behaviour exactly. Proven on this model in poc_session_memory/.
SESSION_MEMORY = os.environ.get("SESSION_MEMORY", "1").lower() not in ("0", "false", "no", "off")
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.expanduser("~/.cache/reachy_voice/sessions"))
SESSION_TTL = float(os.environ.get("SESSION_TTL", "300"))     # idle gap (s) that starts a new conversation
SESSION_WINDOW = int(os.environ.get("SESSION_WINDOW", "40"))  # max messages kept in-context per wake

LOOK_SECONDS = os.environ.get("LOOK_SECONDS", "4")
LISTEN_SECONDS = float(os.environ.get("LISTEN_SECONDS", "8"))  # max window to capture a request
MIC_DEV = os.environ.get("MIC_DEV", "plughw:0")
AUDIO_CARD = os.environ.get("REACHY_AUDIO_CARD", "0")
VOSK_MODEL = os.environ.get("VOSK_MODEL", os.path.expanduser("~/.cache/reachy_voice/vosk-model-small-en-us-0.15"))
COSMOS_PY = os.environ.get("COSMOS_PY", os.path.join(os.path.dirname(__file__), ".venv-cosmos/bin/python"))
# Warm Cosmos vision server (cosmos_server.py). look_and_describe prefers it (model
# already resident -> fast) and falls back to a one-shot subprocess if it's down.
COSMOS_PORT = int(os.environ.get("COSMOS_PORT", "8077"))
COSMOS_URL = os.environ.get("COSMOS_URL", f"http://127.0.0.1:{COSMOS_PORT}")

# Idle human-detection watcher: while resting, every IDLE_INTERVAL seconds grab a
# single image and ask Cosmos who/what it sees, and PRINT it (no robot action yet).
# Fully local ($0). Set IDLE_WATCH=0 to disable.
IDLE_WATCH = os.environ.get("IDLE_WATCH", "1").lower() not in ("0", "false", "no", "off")
IDLE_INTERVAL = float(os.environ.get("IDLE_INTERVAL", "10"))
IDLE_QUESTION = os.environ.get(
    "IDLE_QUESTION",
    "How many people and how many cats can you see? Give the count of each (0 if none) "
    "and a short note on what each is doing.",
)

# Face tracking: an in-process camera owner reads frames continuously, drives the
# head to follow a detected face (local OpenCV Haar cascade — offline, no LLM),
# AND shares the latest frame with Cosmos so the vision checks don't fight the
# camera. Set FACE_TRACK=0 to disable (then the Cosmos server self-captures).
CAMERA = int(os.environ.get("REACHY_CAMERA", "0"))
FACE_TRACK = os.environ.get("FACE_TRACK", "1").lower() not in ("0", "false", "no", "off")
FACE_YAW_SIGN = float(os.environ.get("FACE_YAW_SIGN", "-1"))    # flip if head turns the wrong way
FACE_PITCH_SIGN = float(os.environ.get("FACE_PITCH_SIGN", "1"))  # flip if head tilts the wrong way
FACE_KP_YAW = float(os.environ.get("FACE_KP_YAW", "20"))         # deg per step per unit error
FACE_KP_PITCH = float(os.environ.get("FACE_KP_PITCH", "16"))
FACE_DEADBAND = float(os.environ.get("FACE_DEADBAND", "0.06"))    # ignore errors smaller than this (anti-jitter)
FACE_YAW_MAX = 55.0
FACE_PITCH_MAX = 22.0
FACE_MOVE_PERIOD = float(os.environ.get("FACE_MOVE_PERIOD", "0.1"))  # head-update rate (~10 Hz)
FACE_MOVE_DUR = float(os.environ.get("FACE_MOVE_DUR", "0.12"))       # smoothing duration per head move

# Verbose, timestamped logging of exactly what's happening. Set VERBOSE=0 to quiet.
VERBOSE = os.environ.get("VERBOSE", "1").lower() not in ("0", "false", "no", "off")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def vlog(msg: str) -> None:
    """Granular timestamped trace line (dim grey). Gated by VERBOSE."""
    if VERBOSE:
        print(f"\033[2m[{_ts()}] {msg}\033[0m")

# IoT datalake (AWS S3 Tables / Iceberg, queried via Lambda + Athena). The tools
# resolve Lambda names from this CloudFormation stack, then invoke them.
DATALAKE_REGION = os.environ.get("DATALAKE_REGION", "us-east-1")
DATALAKE_STACK = os.environ.get("DATALAKE_STACK", "iot-datalake")

# AWS IoT Core MQTT trigger (a 2nd wake source alongside "Hey Reachy"). Connects
# over WebSocket + SigV4 using the default AWS credential chain — no certs. The
# listener is DISABLED unless IOT_TOPIC is set, so the default is voice-only.
IOT_ENDPOINT = os.environ.get("IOT_ENDPOINT", "")   # e.g. xxxx-ats.iot.us-east-1.amazonaws.com
IOT_TOPIC = os.environ.get("IOT_TOPIC", "")         # e.g. the-project/reachy-mini/XIAOReachyMini/action
IOT_REGION = os.environ.get("IOT_REGION", DATALAKE_REGION)
IOT_CLIENT_ID = os.environ.get("IOT_CLIENT_ID", f"reachy-mini-{os.getpid()}")
# State/telemetry UPLOAD topic. Each agent action (emotion, presence, vision,
# reply) publishes a full robot-state snapshot here. Defaults to
# "the-project/reachy-mini/XIAOReachyMini/state".
# Publishing reuses the subscribe connection, so it is on whenever the MQTT
# listener is, and a no-op otherwise (voice-only runs are unaffected).
IOT_STATE_TOPIC = os.environ.get("IOT_STATE_TOPIC", "")

# Video upload: while a wake interaction runs, sample the shared camera frames
# into a clip, upload it to S3, and put a presigned DOWNLOAD url in the "reply"
# MQTT message. Only active when the camera owner is up (FACE_TRACK=1) AND MQTT
# is on; otherwise it's a no-op. Frames come from the shared buffer the camera
# owner already publishes (a 2nd VideoCapture would collide with it).
S3_BUCKET = os.environ.get("S3_BUCKET", "")
VIDEO_FPS = float(os.environ.get("VIDEO_FPS", "15"))
VIDEO_MAX_SECONDS = float(os.environ.get("VIDEO_MAX_SECONDS", "120"))  # memory cap
VIDEO_CLIP_SECONDS = float(os.environ.get("VIDEO_CLIP_SECONDS", "30"))  # short clip for presence events
PRESIGNED_URL_EXPIRY = int(os.environ.get("PRESIGNED_URL_EXPIRY", "3600"))  # 1 hour

# Voice: say this prefix right after the wake word to route the rest of the
# sentence straight to the play_emotion tool (e.g. "play emotion, I am happy").
# Without the prefix, the spoken request stays generic (datalake, vision, etc.).
EMOTION_PREFIX = os.environ.get("EMOTION_PREFIX", "play emotion").lower().strip()

# Pricing (USD per 1M tokens) for a rough per-wake cost readout. Override via env.
PRICE_IN = float(os.environ.get("PRICE_IN_PER_M", "0.06"))    # Nova 2 Lite input ~ $0.06/1M
PRICE_OUT = float(os.environ.get("PRICE_OUT_PER_M", "0.24"))  # Nova 2 Lite output ~ $0.24/1M

SYSTEM_PROMPT = (
    "You are Reachy, a small friendly desk robot with a camera. You were just "
    "woken by name and given a spoken request. Choose a tool only if it is needed "
    "to answer.\n"
    "- To SEE or answer anything about the physical scene/room/person in front of "
    "you, call look_and_describe and pass the user's visual question as 'question' "
    "(e.g. 'what color is the mug?'); leave it empty for a general description.\n"
    "- To EXPRESS a feeling, or to react to the sentiment/intent of a message, call "
    "play_emotion with exactly ONE move name chosen from the list provided below. "
    "Match the move to the mood (e.g. praise -> success1 or proud1, bad news -> sad1, "
    "a greeting -> welcoming1, a joke -> laughing1). Never invent a name.\n"
    "- To MOVE or gesture literally, call the motion tools: nod (yes), shake_head "
    "(no), look_around (scan the room), wiggle_antennas, spin_body (turn the body), "
    "or move_head (a deliberate look/tilt). If the request lists SEVERAL motions, "
    "perform ALL of them in order, one tool call each, before you reply. Prefer "
    "play_emotion for a whole mood; use these primitives for literal or directional "
    "motion (e.g. 'nod twice', 'look left', 'spin around').\n"
    "- To answer questions about IoT sensor data, query the data lake. Discover "
    "first, never guess: call list_iot_tables to see what tables exist, then "
    "get_table_schema to see a table's columns, then query_iot_data with the right "
    "table, limit, and an optional SQL WHERE clause. All column values are strings, "
    "so quote them (e.g. motion_detected = 'true').\n"
    "If the request needs no tool, just answer. After using tools, ALWAYS reply "
    "with ONE short, natural spoken sentence stating the answer — never read raw "
    "JSON, table dumps, or column lists aloud. No markdown, no special characters."
)

mini: ReachyMini | None = None
_piper = None          # lazily loaded TTS voice
LAST_LOOK_SEC = 0.0    # set by look_and_describe so we can report vision time
_emotions = None       # lazily loaded recorded-move library (HF)
EMOTION_NAMES: list[str] = []  # the 80 pre-choreographed move names, for the prompt

# Single-consumer queue: the wake-word loop and the MQTT listener both enqueue
# requests; one worker thread drains it so only one task drives the robot at a
# time. Items are (request_text, done_event_or_None). _busy is set while a task
# runs so the voice loop can ignore a wake mid-task.
_task_q: "queue.Queue[tuple[str, threading.Event | None]]" = queue.Queue()
_busy = threading.Event()

# ---- conversational memory: one rotating session id across wakes (feature #4) -- #
# All wakes (voice + MQTT) share one conversation thread so the robot remembers
# recent context; after SESSION_TTL seconds idle the id rotates to a fresh one.
_session_id: str | None = None
_session_seq = 0
_last_interaction = 0.0


def _session_for_now() -> str | None:
    """Return the active conversation id, rotating to a fresh one after SESSION_TTL
    seconds of inactivity. Returns None when SESSION_MEMORY is off (stateless)."""
    global _session_id, _session_seq, _last_interaction
    if not SESSION_MEMORY:
        return None
    now = time.time()
    if _session_id is None or (now - _last_interaction) > SESSION_TTL:
        _session_seq += 1
        _session_id = time.strftime("conv-%Y%m%d-%H%M%S-") + str(_session_seq)
        vlog(f"session memory: starting new conversation {_session_id!r} (window={SESSION_WINDOW})")
    _last_interaction = now
    return _session_id


def _touch_interaction() -> None:
    """Refresh the idle clock at end-of-task so a long task doesn't trigger a
    premature rotation on the next wake (the gap is measured end-to-start)."""
    global _last_interaction
    _last_interaction = time.time()


# ---- AWS IoT MQTT state UPLOAD (publish a snapshot on each action) --------- #
# The live MQTT connection + state topic are set by start_iot_listener once
# connected. publish_state() grabs a full robot-state snapshot and uploads it as
# one JSON message; it runs on a background thread so it never stalls the robot
# worker or the MQTT event loop, and is a no-op when MQTT is off.
_iot_conn = None
_state_topic: str | None = None
_publish_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iot-state")

# Head joint names in the order get_current_joint_positions() returns them
# (matches hardware_config.yaml): body yaw + 6 Stewart-platform actuators.
_HEAD_JOINT_NAMES = ("body_rotation", "stewart_1", "stewart_2", "stewart_3",
                     "stewart_4", "stewart_5", "stewart_6")


def set_iot_connection(conn, topic: str) -> None:
    """Store the live MQTT connection + state topic so actions can upload state."""
    global _iot_conn, _state_topic
    _iot_conn = conn
    _state_topic = topic
    print(f"[iot] state upload enabled -> {topic!r}")


_status_cache: dict | None = None   # cached daemon get_status (static-ish; avoids an RPC per tick)
_status_cache_ts: float = 0.0
_STATUS_TTL = float(os.environ.get("STATE_STATUS_TTL", "5"))


def _read_robot_state() -> dict:
    """Snapshot every hardware/runtime value the Reachy Mini SDK exposes.

    Each section is guarded so one unavailable reading never drops the message.
    IMU is omitted: it is wireless-only and always None on the Lite.
    """
    state: dict = {}
    if mini is None:
        return state
    try:                                       # 9 servo joint positions (rad)
        head, antennas = mini.get_current_joint_positions()
        servos = {name: float(v) for name, v in zip(_HEAD_JOINT_NAMES, head)}
        if len(antennas) == 2:
            servos["right_antenna"] = float(antennas[0])
            servos["left_antenna"] = float(antennas[1])
        state["servos"] = servos
    except Exception as e:  # noqa: BLE001
        vlog(f"state: joints unavailable ({e})")
    try:                                       # head pose -> position + roll/pitch/yaw
        from scipy.spatial.transform import Rotation as _R
        pose = mini.get_current_head_pose()
        state["head_pose"] = {
            "position": [float(x) for x in pose[:3, 3]],
            "rpy": [float(a) for a in _R.from_matrix(pose[:3, :3]).as_euler("xyz")],
        }
    except Exception as e:  # noqa: BLE001
        vlog(f"state: head pose unavailable ({e})")
    try:                                       # daemon status flags (cached; static-ish RPC)
        global _status_cache, _status_cache_ts
        now = time.time()
        if _status_cache is None or now - _status_cache_ts > _STATUS_TTL:
            s = mini.client.get_status()
            backend = getattr(s, "backend_status", None)
            _status_cache = {
                "robot_name": getattr(s, "robot_name", None),
                "version": getattr(s, "version", None),
                "hardware_id": getattr(s, "hardware_id", None),
                "wireless_version": getattr(s, "wireless_version", None),
                "no_media": getattr(s, "no_media", None),
                "media_released": getattr(s, "media_released", None),
                "camera_specs_name": getattr(s, "camera_specs_name", None),
                "wlan_ip": getattr(s, "wlan_ip", None),
                "error": getattr(s, "error", None),
                "backend_ready": getattr(backend, "ready", None),
                "backend_last_alive": getattr(backend, "last_alive", None),
            }
            _status_cache_ts = now
        state["daemon"] = _status_cache
    except Exception as e:  # noqa: BLE001
        vlog(f"state: daemon status unavailable ({e})")
    state["runtime"] = {                       # SDK-side + our flags
        "is_recording": getattr(mini, "is_recording", None),
        "connection_mode": getattr(mini, "connection_mode", None),
        "busy": _busy.is_set(),
        "llm_backend": LLM_BACKEND,
    }
    return state


def publish_state(trigger: str, **fields) -> None:
    """Upload one full robot-state snapshot to the IoT state topic (non-blocking).

    No-op when MQTT is off, so it is always safe to call from any tool. trigger
    is a short tag (emotion/presence/vision/reply); fields add action context.
    """
    if _iot_conn is None or _state_topic is None:
        return
    # Build the full snapshot (reads the robot) and publish ON THE POOL THREAD, so
    # callers on a hot loop (face tracking, ~5 Hz) don't block on daemon reads.
    def _snapshot_and_publish() -> None:
        state = {
            "device": IOT_CLIENT_ID,
            "ts": int(time.time()),
            "trigger": trigger,
            **_read_robot_state(),
            **fields,
        }
        _do_publish(json.dumps(state, separators=(",", ":")))

    _publish_pool.submit(_snapshot_and_publish)


def _do_publish(payload: str) -> None:
    """Send one MQTT message on the background thread (mirrors the subscribe side)."""
    try:
        from awscrt import mqtt
        future, _ = _iot_conn.publish(
            topic=_state_topic, payload=payload, qos=mqtt.QoS.AT_LEAST_ONCE,
        )
        future.result(timeout=5)
        vlog(f"iot upload -> {_state_topic}: {payload[:160]}")
    except Exception as e:  # noqa: BLE001 - telemetry must never kill a task
        vlog(f"iot upload failed: {e}")


# ---- cost guard: hard cap on Bedrock calls per wake ----------------------- #
class ModelCallBudget(HookProvider):
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.count = 0

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before)

    def _before(self, _e: BeforeModelCallEvent) -> None:
        self.count += 1
        print(f"  \033[2m↳ Strands agent → {LLM_BACKEND} model call #{self.count}/{self.max_calls} ({ACTIVE_MODEL})\033[0m")
        if self.count > self.max_calls:
            raise RuntimeError(f"Model-call budget exceeded ({self.max_calls}).")


def _build_model():
    """Build the Strands model for the configured backend (local Nemotron or Bedrock)."""
    if LLM_BACKEND == "bedrock":
        return BedrockModel(model_id=MODEL_ID, region_name=AWS_REGION)
    from strands.models.ollama import OllamaModel
    return OllamaModel(host=OLLAMA_HOST, model_id=NEMOTRON_MODEL)


def _clean_reply(text: str) -> str:
    """Strip any <think>...</think> reasoning a reasoning model (Nemotron) may surface."""
    return re.sub(r"(?is)<think>.*?</think>", "", text or "").strip()


# ---- the vision tool: local Cosmos Reason 2 (warm server, subprocess fallback) #
def _look_via_server(question: str, image: bool = False, image_b64: str | None = None) -> str | None:
    """Ask the warm Cosmos server; return its answer, or None if it's unavailable.

    image_b64 sends a caller-owned frame (shared camera, no server capture).
    image=True (no b64) asks the server to grab a single frame instead of a clip.
    """
    import urllib.request
    try:
        body_in = {"question": question, "seconds": float(LOOK_SECONDS), "image": image}
        if image_b64:
            body_in["image_b64"] = image_b64
        kind = "uploaded-frame" if image_b64 else ("image" if image else "video")
        vlog(f"cosmos -> POST {COSMOS_URL}/look {kind} q={question!r}")
        payload = json.dumps(body_in).encode()
        req = urllib.request.Request(
            f"{COSMOS_URL}/look", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read())
        vlog(f"cosmos <- {body.get('mode','?')} {body.get('seconds','?')}s")
        return body.get("answer") or "(saw nothing)"
    except Exception as e:  # noqa: BLE001 - server down/errored -> caller falls back to subprocess
        vlog(f"cosmos server unavailable ({e})")
        return None


def _look_via_subprocess(question: str) -> str:
    """One-shot fallback: spawn cosmos_describe.py (cold-loads the model)."""
    cmd = [COSMOS_PY, "cosmos_describe.py", "--quiet", "--seconds", str(LOOK_SECONDS)]
    if question:
        cmd += ["--question", question]   # cosmos_describe frames it
    try:
        r = subprocess.run(
            cmd, cwd=os.path.dirname(__file__) or ".",
            capture_output=True, text=True, timeout=300,
        )
        out = (r.stdout or "").strip()
        return out or f"(could not see — {(r.stderr or '')[-200:]})"
    except Exception as e:  # noqa: BLE001
        return f"(camera/vision error: {e})"


@tool
def look_and_describe(question: str = "") -> str:
    """Look through the robot's camera and answer a question about what is seen.

    Use this whenever the request needs the robot's eyes — to see the room, find
    or identify something, read visible text, count things, or check what a person
    is doing. Runs locally on Cosmos Reason 2 (no cloud, $0).

    Args:
        question: What to look for or answer about the scene (e.g. "what color is
            the mug?", "is anyone at the door?", "what is the person doing?").
            Leave empty for a general description of the scene.
    """
    global LAST_LOOK_SEC
    q = (question or "").strip()
    print(f"  \033[2m↳ Strands tool: look_and_describe({q!r}) -> Cosmos Reason 2 (local, $0)...\033[0m")
    t0 = time.time()
    b64 = _latest_jpeg_b64()              # shared frame if the camera owner is up
    if b64:
        answer = _look_via_server(q, image_b64=b64)
    else:
        answer = _look_via_server(q)      # no owner -> server captures a clip itself
    if answer is None:                    # warm server unavailable
        answer = _look_via_subprocess(q)
    LAST_LOOK_SEC = time.time() - t0
    publish_state("vision", vision_question=q, vision_answer=answer)
    return answer


# ---- expressive moves: play one of the 80 pre-choreographed emotions ------ #
def _load_emotions():
    """Load (once) the recorded-move library and cache the move names."""
    global _emotions, EMOTION_NAMES
    if _emotions is None:
        from reachy_mini.motion.recorded_move import RecordedMoves
        _emotions = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
        try:
            EMOTION_NAMES = sorted(_emotions.list_moves())
        except Exception:  # noqa: BLE001
            EMOTION_NAMES = []
    return _emotions


@tool
def list_emotion_moves() -> str:
    """List the names of the pre-choreographed emotion moves available to play."""
    try:
        _load_emotions()
        return ", ".join(EMOTION_NAMES) if EMOTION_NAMES else "(none available)"
    except Exception as e:  # noqa: BLE001
        return f"Emotion library unavailable: {e}"


@tool
def play_emotion(name: str) -> str:
    """Play ONE pre-choreographed emotion move on the robot by name.

    Pick the single move whose mood best matches the request or message sentiment
    (e.g. 'success1', 'proud1', 'sad1', 'welcoming1', 'laughing1', 'curious1').

    Args:
        name: An exact move name from list_emotion_moves. Do not invent names.

    Returns:
        Confirmation, or — if the name is invalid — the list of valid names to retry with.
    """
    print(f"  \033[2m↳ Strands tool: play_emotion({name!r}) -> recorded move...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        moves = _load_emotions()
        if EMOTION_NAMES and name not in EMOTION_NAMES:
            return f"'{name}' is not a valid move. Choose one of: {', '.join(EMOTION_NAMES)}"
        mini.play_move(moves.get(name), initial_goto_duration=1.0, sound=False)
        publish_state("emotion", emotion_name=name)
        return f"Played '{name}'."
    except Exception as e:  # noqa: BLE001
        return f"Could not play '{name}': {e}"


# ---- expressive motion: compose primitive moves on head / body / antennas --- #
# Each tool wraps the Reachy SDK, clamps to a safe range, returns to neutral where
# it makes sense, and returns a short status string the agent reads back. Safe to
# call during a wake task: the face tracker is paused while _busy is set, and
# handle_wake recenters the head afterwards, so nothing else drives the motors.
@tool
def nod(times: int = 2) -> str:
    """Nod the head up and down to say 'yes' or to acknowledge.

    Args:
        times: How many nods (clamped 1-5).
    """
    print(f"  \033[2m↳ Strands tool: nod(times={times}) -> head...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        n = max(1, min(int(times), 5))
        for _ in range(n):
            mini.goto_target(create_head_pose(pitch=15, degrees=True), duration=0.35)
            mini.goto_target(create_head_pose(pitch=-10, degrees=True), duration=0.35)
        mini.goto_target(INIT_HEAD_POSE, duration=0.35)
        publish_state("motion", motion="nod", times=n)
        return f"Nodded {n} time(s)."
    except Exception as e:  # noqa: BLE001
        return f"Could not nod: {e}"


@tool
def shake_head(times: int = 2) -> str:
    """Shake the head left and right to say 'no'.

    Args:
        times: How many shakes (clamped 1-5).
    """
    print(f"  \033[2m↳ Strands tool: shake_head(times={times}) -> head...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        n = max(1, min(int(times), 5))
        for _ in range(n):
            mini.goto_target(create_head_pose(yaw=25, degrees=True), duration=0.35)
            mini.goto_target(create_head_pose(yaw=-25, degrees=True), duration=0.35)
        mini.goto_target(INIT_HEAD_POSE, duration=0.35)
        publish_state("motion", motion="shake_head", times=n)
        return f"Shook head {n} time(s)."
    except Exception as e:  # noqa: BLE001
        return f"Could not shake head: {e}"


@tool
def look_around() -> str:
    """Sweep the head left, right, and back to center to scan the room."""
    print("  \033[2m↳ Strands tool: look_around() -> head sweep...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        mini.goto_target(create_head_pose(yaw=60, degrees=True), duration=1.0)
        mini.goto_target(create_head_pose(yaw=-60, degrees=True), duration=1.5)
        mini.goto_target(INIT_HEAD_POSE, duration=1.0)
        publish_state("motion", motion="look_around")
        return "Looked around the room."
    except Exception as e:  # noqa: BLE001
        return f"Could not look around: {e}"


@tool
def wiggle_antennas(times: int = 3) -> str:
    """Wiggle both antennas up and down expressively.

    Args:
        times: How many wiggles (clamped 1-6).
    """
    print(f"  \033[2m↳ Strands tool: wiggle_antennas(times={times}) -> antennas...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        n = max(1, min(int(times), 6))
        up, down = [0.5, 0.5], [-0.5, -0.5]   # radians
        for _ in range(n):
            mini.goto_target(antennas=up, duration=0.25)
            mini.goto_target(antennas=down, duration=0.25)
        mini.goto_target(antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=0.25)
        publish_state("motion", motion="wiggle_antennas", times=n)
        return f"Wiggled antennas {n} time(s)."
    except Exception as e:  # noqa: BLE001
        return f"Could not wiggle antennas: {e}"


@tool
def spin_body(degrees: float = 90.0, duration: float = 1.5) -> str:
    """Rotate the whole body around its vertical axis.

    Args:
        degrees: Rotation angle, clamped to about -160..160.
        duration: Seconds for the smooth move.
    """
    print(f"  \033[2m↳ Strands tool: spin_body(degrees={degrees}) -> body...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        d = _clamp(float(degrees), -160.0, 160.0)
        mini.goto_target(body_yaw=math.radians(d), duration=max(0.3, float(duration)))
        publish_state("motion", motion="spin_body", degrees=d)
        return f"Rotated body to {d:.0f} degrees."
    except Exception as e:  # noqa: BLE001
        return f"Could not spin body: {e}"


@tool
def move_head(pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0, duration: float = 1.0) -> str:
    """Move the head to an absolute orientation in degrees (directional looks/tilts).

    For 'yes'/'no' prefer nod/shake_head; to scan the room prefer look_around. Use
    this for a deliberate look up/down/sideways or a curious head tilt.

    Args:
        pitch: Up/down tilt — negative looks up, positive looks down (clamped -40..40).
        roll: Sideways head tilt (clamped -40..40).
        yaw: Left/right turn (clamped -180..180).
        duration: Seconds for the smooth move.
    """
    print(f"  \033[2m↳ Strands tool: move_head(pitch={pitch}, roll={roll}, yaw={yaw}) -> head...\033[0m")
    if mini is None:
        return "Robot not connected."
    try:
        p = _clamp(float(pitch), -40.0, 40.0)
        ro = _clamp(float(roll), -40.0, 40.0)
        y = _clamp(float(yaw), -180.0, 180.0)
        mini.goto_target(create_head_pose(roll=ro, pitch=p, yaw=y, degrees=True), duration=max(0.3, float(duration)))
        publish_state("motion", motion="move_head", pitch=p, roll=ro, yaw=y)
        return f"Head moved to pitch={p:.0f}, roll={ro:.0f}, yaw={y:.0f} degrees."
    except Exception as e:  # noqa: BLE001
        return f"Could not move head: {e}"


# ---- IoT datalake access (AWS S3 Tables / Iceberg via Lambda + Athena) ----- #
def _get_lambda_name(output_key: str) -> str:
    """Resolve a Lambda function name from the datalake CloudFormation stack outputs."""
    result = subprocess.run(
        [
            "aws", "cloudformation", "describe-stacks",
            "--stack-name", DATALAKE_STACK,
            "--region", DATALAKE_REGION,
            "--query", "Stacks[0].Outputs",
            "--output", "json",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Stack {DATALAKE_STACK} not found: {result.stderr.strip()}")
    outputs = json.loads(result.stdout)
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == output_key)


def _invoke_lambda(function_name: str, payload: dict) -> dict:
    """Invoke a datalake Lambda function and return the parsed JSON body."""
    tmpfile = f"/tmp/reachy-lambda-{os.getpid()}.json"
    try:
        result = subprocess.run(
            [
                "aws", "lambda", "invoke",
                "--function-name", function_name,
                "--payload", json.dumps(payload),
                "--region", DATALAKE_REGION,
                "--cli-binary-format", "raw-in-base64-out",
                tmpfile,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Lambda invoke failed: {result.stderr.strip()}")
        with open(tmpfile) as f:
            response = json.load(f)
        body = json.loads(response.get("body", "{}"))
        if response.get("statusCode") != 200:
            raise RuntimeError(f"Lambda error: {json.dumps(body)}")
        return body
    finally:
        if os.path.exists(tmpfile):
            os.unlink(tmpfile)


@tool
def list_iot_tables() -> str:
    """List all available IoT device tables with row counts and last ingestion times.

    Use this FIRST to discover what tables exist before querying them.

    Returns:
        JSON string with table names, row counts, last ingestion times, and the
        total row count across all tables.
    """
    print("  \033[2m↳ Strands tool: list_iot_tables -> datalake (Lambda/Athena)...\033[0m")
    try:
        return json.dumps(_invoke_lambda(_get_lambda_name("TableStatsFunctionName"), {}), indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@tool
def get_table_schema(table: str) -> str:
    """Get the column names and sample values for a specific IoT table.

    Use this to discover available columns before querying with WHERE filters.

    Args:
        table: The table name (e.g. water_leak_detector, presence, environment_monitor).

    Returns:
        JSON string listing each column name with its sample value.
    """
    print(f"  \033[2m↳ Strands tool: get_table_schema({table!r}) -> datalake...\033[0m")
    try:
        body = _invoke_lambda(_get_lambda_name("QueryFunctionName"), {"table": table, "limit": 1, "where": ""})
        rows = body.get("data", [])
        if not rows:
            return json.dumps({"table": table, "columns": [], "note": "Table is empty"})
        schema = {"table": table, "columns": [{"name": c, "sample_value": v} for c, v in rows[0].items()]}
        return json.dumps(schema, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


@tool
def query_iot_data(table: str, limit: int = 20, where: str = "") -> str:
    """Query rows from a specific IoT device table.

    Call get_table_schema first to see available columns for filtering.

    Args:
        table: The table name to query (use list_iot_tables to see available tables).
        limit: Maximum number of rows to return (default 20).
        where: Optional SQL WHERE clause (e.g. "water_detected = 'true'"). All
               values are strings, so use single quotes around them.

    Returns:
        JSON string with the query results including rows and row count.
    """
    print(f"  \033[2m↳ Strands tool: query_iot_data({table!r}, limit={limit}, where={where!r}) -> datalake...\033[0m")
    try:
        body = _invoke_lambda(_get_lambda_name("QueryFunctionName"), {"table": table, "limit": limit, "where": where})
        return json.dumps(body, indent=2)
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


# ---- local text-to-speech through the Reachy speaker ---------------------- #
def speak(text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    print(f"\033[1;35m[reachy says]\033[0m {text}")
    wav = "/tmp/reachy_say.wav"
    if _piper is not None:
        try:
            with wave.open(wav, "wb") as wf:
                _piper.synthesize_wav(text, wf)  # sets WAV format itself
            _aplay(wav)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[tts] piper failed ({e}); trying espeak-ng")
    if shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-w", wav, text], check=False)
        _aplay(wav)
        return
    print("[tts] no working TTS (install piper-tts or `sudo apt install espeak-ng`) — printed only.")


def _aplay(wav: str) -> None:
    subprocess.run(["aplay", "-q", "-D", f"plughw:{AUDIO_CARD}", wav], check=False)


def _load_piper() -> None:
    global _piper
    voice = os.environ.get("PIPER_VOICE", "")
    if not voice or not os.path.exists(voice):
        return
    try:
        from piper import PiperVoice
        _piper = PiperVoice.load(voice)
        print(f"[tts] piper voice loaded: {voice}")
    except Exception as e:  # noqa: BLE001
        print(f"[tts] piper unavailable ({e}); will use espeak-ng if present")


def _print_summary(result, budget: ModelCallBudget, t_wall: float) -> None:
    m = getattr(result, "metrics", None)
    usage = dict(getattr(m, "accumulated_usage", {}) or {})
    acc = dict(getattr(m, "accumulated_metrics", {}) or {})
    in_t = int(usage.get("inputTokens", 0))
    out_t = int(usage.get("outputTokens", 0))
    tot = int(usage.get("totalTokens", in_t + out_t))
    cache_r = int(usage.get("cacheReadInputTokens", 0))
    local = LLM_BACKEND == "ollama"
    cost = 0.0 if local else in_t / 1e6 * PRICE_IN + out_t / 1e6 * PRICE_OUT
    cost_note = "(local Nemotron on Thor GPU, $0)" if local else ""
    print("\033[1;33m===== Strands agent — wake task summary =====\033[0m")
    print(f"  framework        : Strands Agents (strandsagents.com)")
    print(f"  backend / model  : {LLM_BACKEND} / {ACTIVE_MODEL}")
    print(f"  model calls      : {budget.count} / {MAX_MODEL_CALLS}")
    print(f"  tokens           : in={in_t}  out={out_t}  total={tot}  cache_read={cache_r}")
    print(f"  est. llm cost    : ${cost:.5f} {cost_note}")
    print(f"  llm latency      : {acc.get('latencyMs', '?')} ms")
    print(f"  cycles           : {getattr(m, 'cycle_count', '?')}")
    print(f"  vision (cosmos)  : {LAST_LOOK_SEC:.1f} s  (local on Thor GPU, $0)")
    print(f"  wall time        : {time.time() - t_wall:.1f} s")
    print("\033[1;33m=============================\033[0m")


# ---- after wake: transcribe ONE spoken request (offline, $0) -------------- #
def listen_for_command(stream, vmodel) -> str:
    """Capture and transcribe the user's request after the wake word.

    Reuses the already-running arecord stream and a FRESH Vosk recognizer (so the
    wake word itself isn't carried over). Returns when Vosk reports end-of-speech
    (a natural pause) with non-empty text, or after LISTEN_SECONDS as a backstop.
    Still fully offline — no Bedrock, no cost.
    """
    rec = KaldiRecognizer(vmodel, 16000)
    print(f"\033[1;36m[listen]\033[0m head up — go ahead (up to {LISTEN_SECONDS:.0f}s)...")
    t_end = time.time() + LISTEN_SECONDS
    while time.time() < t_end:
        data = stream.stdout.read(4000)
        if not data:
            break
        if rec.AcceptWaveform(data):           # end-of-utterance (trailing silence)
            text = json.loads(rec.Result()).get("text", "").strip()
            if text:                            # got a full phrase -> done
                return text
        else:
            partial = json.loads(rec.PartialResult()).get("partial", "").strip()
            if partial:
                print(f"\033[2m  …{partial}\033[0m", end="\r")
    # window elapsed without a clean end -> flush whatever was heard
    return json.loads(rec.FinalResult()).get("text", "").strip()


# ---- head pose: keep the head up the whole time the assistant runs -------- #
def _head_up(duration: float = 1.5) -> None:
    """Enable motors and hold the upright neutral pose (the always-on resting pose).

    Used instead of goto_sleep so the head stays up for the entire session — on
    startup and after every task — rather than dropping between wakes.
    """
    if mini is None:
        return
    try:
        vlog(f"head -> upright neutral ({duration:.1f}s)")
        mini.enable_motors()
        mini.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=duration)
    except Exception as e:  # noqa: BLE001
        vlog(f"head move failed: {e}")


# ---- route a request to the emotion tool (shared by voice + MQTT message) -- #
def _emotion_request(sentiment: str) -> str:
    """Wrap a sentiment sentence into an instruction to pick + play ONE emotion move."""
    return (
        f'Act this out with one move: "{sentiment}". Read its sentiment/intent, then '
        "call play_emotion with the single best-matching move name. You may also say "
        "one short sentence."
    )


def _look_request(question: str) -> str:
    """Wrap a visual question into an instruction to look via the camera and answer."""
    if question:
        return (
            f'Look through your camera and answer this: "{question}". Call '
            "look_and_describe with that question, then say the answer in one short sentence."
        )
    return (
        "Look through your camera and describe what you see. Call look_and_describe, "
        "then say it in one short sentence."
    )


def _move_request(instruction: str) -> str:
    """Wrap a motion instruction into a request to compose the motion primitives.

    Shared by voice (passthrough already reaches the same agent) and the MQTT
    {"event":"move"} route, so a physical gesture is a first-class trigger like
    look/emotion. The agent has nod/shake_head/look_around/wiggle_antennas/
    spin_body/move_head and may chain a few.
    """
    if instruction:
        return (
            f'Move your body to do this, performing EVERY step in the order given: '
            f'"{instruction}". Call one motion tool per step (nod, shake_head, '
            "look_around, wiggle_antennas, spin_body, move_head) and do not stop or "
            "reply until all steps are done — then say one short sentence."
        )
    return (
        "Do a short, lively gesture with your motion tools (nod, shake_head, "
        "look_around, wiggle_antennas, spin_body), then say one short sentence."
    )


def _route_voice_request(text: str) -> str:
    """Spoken request router: the EMOTION_PREFIX triggers play_emotion; else pass through.

    "play emotion, I am happy" -> emotion task on the sentiment "I am happy".
    Anything else is returned unchanged so the agent stays fully generic.
    """
    stripped = text.strip()
    if EMOTION_PREFIX and stripped.lower().startswith(EMOTION_PREFIX):
        sentiment = stripped[len(EMOTION_PREFIX):].lstrip(" ,.:;-").strip()
        print(f"  \033[2m↳ emotion trigger -> sentiment: {sentiment!r}\033[0m")
        return _emotion_request(sentiment or stripped)
    return text


# ---- the per-wake task: fresh agent -> task -> destroy -------------------- #
def handle_wake(request: str) -> None:
    assert mini is not None
    t_wall = time.time()
    print(f"\033[1;36m[wake]\033[0m request: {request!r} -> spinning up \033[1mStrands agent\033[0m...")

    # Inject the valid move names so the agent picks a real one in a single shot
    # (no extra discovery round-trip). Falls back to the tool if loading failed.
    sys_prompt = SYSTEM_PROMPT
    if EMOTION_NAMES:
        sys_prompt += "\n\nValid play_emotion move names: " + ", ".join(EMOTION_NAMES) + "."

    vlog(f"Strands Agents: building Agent(backend={LLM_BACKEND}, model={ACTIVE_MODEL}, cap={MAX_MODEL_CALLS}) "
         f"with tools=[look_and_describe, play_emotion, list_emotion_moves, nod, shake_head, "
         f"look_around, wiggle_antennas, spin_body, move_head, list_iot_tables, "
         f"get_table_schema, query_iot_data]")
    # Record the whole interaction (start -> end) so the reply message can carry
    # a presigned download URL for the clip. No-op when camera/MQTT unavailable.
    recording = start_recording()
    budget = ModelCallBudget(MAX_MODEL_CALLS)
    agent_kwargs = dict(
        model=_build_model(),
        system_prompt=sys_prompt,
        tools=[look_and_describe, play_emotion, list_emotion_moves,
               nod, shake_head, look_around, wiggle_antennas, spin_body, move_head,
               list_iot_tables, get_table_schema, query_iot_data],
        hooks=[budget],
    )
    # Feature #4: attach the persisted conversation so this fresh agent recalls
    # recent turns. SlidingWindow bounds replayed context (cost/latency); the id
    # rotates after SESSION_TTL idle. session_id is None -> stateless, as before.
    session_id = _session_for_now()
    if session_id is not None:
        agent_kwargs.update(
            agent_id="reachy",
            session_manager=FileSessionManager(session_id=session_id, storage_dir=SESSION_DIR),
            conversation_manager=SlidingWindowConversationManager(window_size=SESSION_WINDOW),
        )
        vlog(f"session memory: conversation={session_id} dir={SESSION_DIR}")
    agent = Agent(**agent_kwargs)
    try:
        result = agent(request)
        _print_summary(result, budget, t_wall)
        reply = _clean_reply(str(result))
        speak(reply)
        video_url = stop_recording_and_upload() if recording else None
        publish_state("reply", request=request, reply=reply,
                      **({"video_url": video_url} if video_url else {}))
    except Exception as e:  # noqa: BLE001
        print(f"[agent] error: {e}")
        speak("Sorry, I had trouble with that.")
    finally:
        if recording:
            stop_recording()   # idempotent: ensure the capture thread is stopped
        del agent          # destroy the agent instance
        gc.collect()
        print("[wake] Strands agent destroyed; head stays up, ready for the next wake.\n")

    _head_up()  # return to upright neutral (head stays up, not asleep)
    _touch_interaction()  # measure the idle gap from end-of-task for session rotation


# ---- single worker that owns the robot (drains the request queue) --------- #
def _worker_loop() -> None:
    """Run queued requests one at a time so voice + MQTT never collide on the robot."""
    while True:
        request, done = _task_q.get()
        _busy.set()
        vlog(f"worker: picked up task -> {request!r}")
        try:
            if request:
                handle_wake(request)
        except Exception as e:  # noqa: BLE001 - never let one task kill the worker
            print(f"[worker] task error: {e}")
        finally:
            _busy.clear()
            vlog("worker: task done; idle again")
            if done is not None:
                done.set()        # unblock the voice loop so it can reset the mic
            _task_q.task_done()


# ---- single camera owner: stream frames -> head tracking + shared with Cosmos #
_cam_lock = threading.Lock()
_latest_frame = None                 # most recent BGR frame (numpy array)
_cam_active = threading.Event()      # set once the camera is open and streaming


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _capture_loop() -> None:
    """Keep _latest_frame fresh for all in-process consumers (face tracking, the
    Cosmos look path, the clip recorder).

    Prefers the media-bus camera broker: when it is up, this is just one more
    subscriber, so external processes can read the same camera concurrently. If
    no broker is running, falls back to owning /dev/video<CAMERA> directly (MJPG,
    so even standalone runs get ~30fps instead of YUYV's ~5).
    """
    global _latest_frame

    if media_bus.broker_available("camera"):
        vlog(f"camera owner: subscribing to media-bus broker at {media_bus.CAM_SOCK}")
        _cam_active.set()
        try:
            for frame in media_bus.camera_frames():
                with _cam_lock:
                    _latest_frame = frame
        except Exception as e:  # noqa: BLE001
            vlog(f"camera owner: broker subscription ended ({e})")
            _cam_active.clear()
        return

    try:
        import cv2
    except Exception as e:  # noqa: BLE001
        vlog(f"camera owner: opencv unavailable ({e}); shared camera off (server self-captures)")
        return
    cap = cv2.VideoCapture(CAMERA, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    if not cap.isOpened():
        vlog(f"camera owner: cannot open camera {CAMERA}; shared camera off")
        return
    _cam_active.set()
    vlog(f"camera owner: holding /dev/video{CAMERA} directly (no broker), streaming frames")
    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.01)
            continue
        with _cam_lock:
            _latest_frame = frame


def _tracker_loop() -> None:
    """Follow a detected face with the head (local Haar cascade — offline, $0)."""
    if not FACE_TRACK:
        vlog("face-track: disabled (FACE_TRACK=0)")
        return
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return
    for _ in range(100):                 # wait for the camera owner to come up
        if _cam_active.is_set():
            break
        time.sleep(0.1)
    if not _cam_active.is_set():
        vlog("face-track: camera owner never came up; tracking off")
        return
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    vlog("face-track: following faces -> head")
    tyaw = tpitch = 0.0
    while True:
        time.sleep(FACE_MOVE_PERIOD)
        if _busy.is_set():
            tyaw = tpitch = 0.0          # a task owns the head; recenter for when we resume
            continue
        with _cam_lock:
            frame = _latest_frame
        if frame is None:
            continue
        small = cv2.resize(frame, (320, 240))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 4, minSize=(30, 30))
        if len(faces) == 0:
            continue
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])   # largest = nearest face
        ex = ((x + w / 2) - 160) / 160.0                     # horizontal error [-1, 1]
        ey = ((y + h / 2) - 120) / 120.0                     # vertical error   [-1, 1]
        moved = False
        if abs(ex) > FACE_DEADBAND:
            tyaw = _clamp(tyaw + FACE_KP_YAW * ex * FACE_YAW_SIGN, -FACE_YAW_MAX, FACE_YAW_MAX)
            moved = True
        if abs(ey) > FACE_DEADBAND:
            tpitch = _clamp(tpitch + FACE_KP_PITCH * ey * FACE_PITCH_SIGN, -FACE_PITCH_MAX, FACE_PITCH_MAX)
            moved = True
        if moved and mini is not None:
            try:
                mini.goto_target(create_head_pose(yaw=tyaw, pitch=tpitch, degrees=True), duration=FACE_MOVE_DUR)
                vlog(f"face-track: err=({ex:.2f},{ey:.2f}) -> head yaw={tyaw:.0f} pitch={tpitch:.0f}")
                # Publish a full state snapshot to AWS IoT for every face-track row.
                publish_state("face_track", yaw=round(tyaw, 1), pitch=round(tpitch, 1),
                              err_x=round(ex, 3), err_y=round(ey, 3))
            except Exception as e:  # noqa: BLE001
                vlog(f"face-track: head move failed: {e}")


def _latest_jpeg_b64() -> str | None:
    """Encode the most recent frame as base64 JPEG, or None if no camera owner."""
    if not _cam_active.is_set():
        return None
    try:
        import base64
        import cv2
        with _cam_lock:
            frame = _latest_frame
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode("ascii") if ok else None
    except Exception as e:  # noqa: BLE001
        vlog(f"frame encode failed: {e}")
        return None


# ---- video clip of an interaction -> S3 -> presigned download URL ---------- #
# Samples the shared camera buffer (no 2nd VideoCapture) for the duration of a
# wake interaction, encodes to H.264/avc1 (browser-playable) via ffmpeg, uploads
# to S3, and returns a presigned download URL for the "reply" MQTT message.
_rec_stop = threading.Event()
_rec_thread: threading.Thread | None = None
_rec_frames: list = []
_rec_audio_handle = None        # (stop_event, thread, buffer) for the parallel mic capture
_rec_audio_bytes: bytes = b""   # PCM captured alongside the last clip


def _start_audio_capture():
    """Begin buffering mic PCM from the audio broker (fan-out), in parallel with the
    frame capture. Returns a handle, or None if the mic broker isn't available."""
    if not media_bus.broker_available("audio"):
        return None
    buf = bytearray()
    stop = threading.Event()
    max_bytes = int(media_bus.AUDIO_RATE * media_bus.AUDIO_SAMPLE_BYTES *
                    media_bus.AUDIO_CHANNELS * VIDEO_MAX_SECONDS)

    def _loop() -> None:
        try:
            mic = media_bus.MicReader()      # own subscription; coexists with the voice loop
        except Exception as e:  # noqa: BLE001
            vlog(f"video: audio capture unavailable ({e})")
            return
        try:
            while not stop.is_set() and len(buf) < max_bytes:
                chunk = mic.stdout.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)   # in-place; do NOT rebind buf (closure var)
        finally:
            mic.terminate()

    t = threading.Thread(target=_loop, name="video-aud", daemon=True)
    t.start()
    vlog("video: also capturing audio from the mic broker")
    return (stop, t, buf)


def _stop_audio_capture(handle) -> bytes:
    if not handle:
        return b""
    stop, t, buf = handle
    stop.set()
    t.join(timeout=5)
    return bytes(buf)


def start_recording() -> bool:
    """Begin sampling the shared camera into a clip. Returns False if unavailable.

    Records only when the camera owner is streaming (FACE_TRACK=1) and MQTT is on
    (the URL has somewhere to go). Otherwise it's a no-op and video is skipped.
    """
    global _rec_thread, _rec_audio_handle
    if _iot_conn is None or not _cam_active.is_set():
        return False

    def _record_loop() -> None:
        max_frames = int(VIDEO_FPS * VIDEO_MAX_SECONDS)
        period = 1.0 / VIDEO_FPS
        while not _rec_stop.is_set() and len(_rec_frames) < max_frames:
            with _cam_lock:
                frame = _latest_frame
            if frame is not None:
                _rec_frames.append(frame.copy())
            time.sleep(period)

    _rec_frames.clear()
    _rec_stop.clear()
    _rec_thread = threading.Thread(target=_record_loop, name="video-rec", daemon=True)
    _rec_thread.start()
    _rec_audio_handle = _start_audio_capture()   # parallel mic capture (if broker up)
    vlog("video: recording interaction (sampling shared camera)")
    return True


def stop_recording() -> None:
    """Stop the capture thread (idempotent; safe on the error path)."""
    global _rec_thread, _rec_audio_handle, _rec_audio_bytes
    _rec_stop.set()
    if _rec_thread is not None:
        _rec_thread.join(timeout=5)
        _rec_thread = None
    _rec_audio_bytes = _stop_audio_capture(_rec_audio_handle)
    _rec_audio_handle = None


def _ffmpeg_exe() -> str | None:
    """Resolve an ffmpeg binary: prefer imageio-ffmpeg's bundled build (has libx264),
    else a system ffmpeg. None if neither is available."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _encode_and_upload(frames: list, audio_pcm: bytes = b"") -> str | None:
    """Encode BGR frames (+ optional mic PCM) to a browser-playable MP4, upload to
    S3, return a presigned URL.

    OpenCV's mp4v writer produces MPEG-4 Part 2 (fourcc mp4v), which no browser can
    decode in <video>. So we write the raw clip with OpenCV, then transcode to H.264
    (avc1), yuv420p, baseline + faststart with ffmpeg before upload. When mic audio
    was captured (media bus), it is muxed in as AAC.
    """
    if not frames:
        vlog("video: no frames captured — skipping upload")
        return None
    import tempfile
    raw_path = final_path = wav_path = None
    try:
        import cv2
        stamp = time.strftime("%Y%m%d_%H%M%S")
        h, w = frames[0].shape[:2]

        raw = tempfile.NamedTemporaryFile(suffix=".mp4", prefix=f"reachy_raw_{stamp}_", delete=False)
        raw_path = raw.name
        raw.close()
        writer = cv2.VideoWriter(raw_path, cv2.VideoWriter_fourcc(*"mp4v"), VIDEO_FPS, (w, h))
        for f in frames:
            writer.write(f)
        writer.release()

        # Transcode to H.264 (avc1) so it plays in Firefox/Chrome/Safari, muxing
        # the mic audio (if any) as AAC.
        upload_path = raw_path
        ff = _ffmpeg_exe()
        if ff:
            final = tempfile.NamedTemporaryFile(suffix=".mp4", prefix=f"reachy_{stamp}_", delete=False)
            final_path = final.name
            final.close()

            inputs = ["-i", raw_path]
            audio_args = ["-an"]
            if audio_pcm:
                wf_tmp = tempfile.NamedTemporaryFile(suffix=".wav", prefix=f"reachy_aud_{stamp}_", delete=False)
                wav_path = wf_tmp.name
                wf_tmp.close()
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(media_bus.AUDIO_CHANNELS)
                    wf.setsampwidth(media_bus.AUDIO_SAMPLE_BYTES)
                    wf.setframerate(media_bus.AUDIO_RATE)
                    wf.writeframes(audio_pcm)
                inputs += ["-i", wav_path]
                audio_args = ["-c:a", "aac", "-b:a", "96k", "-shortest"]

            cmd = [
                ff, "-y", *inputs,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline", "-level", "3.1",
                *audio_args, "-movflags", "+faststart", final_path,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.getsize(final_path) > 0:
                upload_path = final_path
                vlog(f"video: transcoded to H.264 (avc1) + faststart"
                     f"{' + AAC audio' if audio_pcm else ''}")
            else:
                vlog(f"video: ffmpeg transcode failed (rc={r.returncode}); uploading mp4v fallback "
                     f"(may not play in browser): {(r.stderr or '')[-200:]}")
        else:
            vlog("video: ffmpeg unavailable (pip install imageio-ffmpeg) — uploading mp4v, "
                 "which browsers cannot decode.")

        if not S3_BUCKET:
            vlog("video: S3_BUCKET unset — skipping upload (set it to enable clip upload)")
            return None

        import boto3
        key = f"videos/reachy_{stamp}.mp4"
        s3 = boto3.client("s3")  # same as the hand sibling project (works there)
        s3.upload_file(upload_path, S3_BUCKET, key)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=PRESIGNED_URL_EXPIRY,
        )
        vlog(f"video: uploaded {len(frames)} frames -> s3://{S3_BUCKET}/{key}")
        return url
    except Exception as e:  # noqa: BLE001 - video must never break an interaction
        vlog(f"video: encode/upload failed: {e}")
        return None
    finally:
        for p in (raw_path, final_path, wav_path):
            if p:
                try:
                    os.remove(p)
                except Exception:  # noqa: BLE001
                    pass


def stop_recording_and_upload() -> str | None:
    """Stop the interaction recording, encode, upload, return a presigned URL."""
    stop_recording()
    frames = list(_rec_frames)
    _rec_frames.clear()
    return _encode_and_upload(frames, _rec_audio_bytes)


def record_clip_and_upload(seconds: float = VIDEO_CLIP_SECONDS) -> str | None:
    """Record a short clip NOW (synchronous), upload it, return a presigned URL.

    Used for instantaneous events (e.g. presence) that have no interaction window
    to wrap. No-op (None) when the camera owner or MQTT is unavailable.
    """
    if _iot_conn is None or not _cam_active.is_set():
        return None
    audio_handle = _start_audio_capture()    # parallel mic capture (if broker up)
    frames = []
    max_frames = int(VIDEO_FPS * seconds)
    period = 1.0 / VIDEO_FPS
    vlog(f"video: capturing {seconds:.0f}s clip (sampling shared camera)")
    while len(frames) < max_frames:
        with _cam_lock:
            frame = _latest_frame
        if frame is not None:
            frames.append(frame.copy())
        time.sleep(period)
    audio = _stop_audio_capture(audio_handle)
    return _encode_and_upload(frames, audio)


# ---- idle presence: Cosmos observation -> Strands agent -> tool (logs only) - #
@tool
def report_human_presence(people: int, description: str) -> str:
    """Report that one or more PEOPLE (humans) are currently visible to the robot.

    Call this whenever at least one human is seen in the observation.

    Args:
        people: how many humans are visible.
        description: a brief note on who is seen and what they are doing.
    """
    # For now this only LOGS — it's the hook for a human-specific action later.
    print(f"\033[1;35m[{_ts()}] [human-presence]\033[0m people={people} :: {description}")
    if people >= 1:  # only upload when something is actually detected
        video_url = record_clip_and_upload()
        publish_state("presence", presence_kind="human", presence_count=people,
                      presence_description=description,
                      **({"video_url": video_url} if video_url else {}))
    return "logged"


@tool
def report_cat_presence(cats: int, description: str) -> str:
    """Report that one or more CATS are currently visible to the robot.

    Call this whenever at least one cat is seen in the observation.

    Args:
        cats: how many cats are visible.
        description: a brief note on what the cat is doing.
    """
    # For now this only LOGS — it's the hook for a cat-specific action later.
    print(f"\033[1;36m[{_ts()}] [cat-presence]\033[0m cats={cats} :: {description}")
    if cats >= 1:  # only upload when something is actually detected
        video_url = record_clip_and_upload()
        publish_state("presence", presence_kind="cat", presence_count=cats,
                      presence_description=description,
                      **({"video_url": video_url} if video_url else {}))
    return "logged"


def _run_presence_agent(observation: str) -> None:
    """Hand the idle camera observation to a Strands agent that calls the right tool(s).

    Humans -> report_human_presence, cats -> report_cat_presence, both -> both, none -> nothing.
    """
    agent = None
    try:
        agent = Agent(
            model=_build_model(),
            tools=[report_human_presence, report_cat_presence],
            system_prompt=(
                "You receive a one-line observation from a robot's camera. "
                "Call report_human_presence if one or more PEOPLE (humans) are present. "
                "Call report_cat_presence if one or more CATS are present. "
                "If BOTH humans and cats are present, call BOTH tools. "
                "If neither is present, do nothing. Do not produce any other prose."
            ),
        )
        vlog("presence: handing observation to Strands agent (-> human/cat presence tools)")
        agent(f"Camera observation: {observation}")
    except Exception as e:  # noqa: BLE001
        vlog(f"presence agent error: {e}")
    finally:
        del agent
        gc.collect()


# ---- idle human-detection watcher (single image -> Cosmos -> Strands tool) -- #
def _idle_watcher() -> None:
    """While resting, periodically grab one frame, see who Cosmos sees, and route
    that observation to a Strands agent that calls report_presence (logs for now).

    Skips ticks while a task is running (so it never competes with a real look or
    drives the GPU mid-task). $0 — local Cosmos + local Nemotron.
    """
    if not IDLE_WATCH:
        vlog("idle watcher: disabled (IDLE_WATCH=0)")
        return
    vlog(f"idle watcher: on — every {IDLE_INTERVAL:.0f}s, single-image presence check -> Strands tool")
    while True:
        time.sleep(IDLE_INTERVAL)
        if _busy.is_set():
            vlog("idle watcher: skip tick (busy with a task)")
            continue
        t0 = time.time()
        ans = _look_via_server(IDLE_QUESTION, image=True, image_b64=_latest_jpeg_b64())
        dt = time.time() - t0
        if ans is None:
            print(f"\033[2m[{_ts()}] [idle-watch] cosmos server unavailable\033[0m")
            continue
        print(f"\033[1;34m[{_ts()}] [idle-watch {dt:.1f}s]\033[0m {ans}")
        _run_presence_agent(ans)   # Strands agent -> report_presence tool (logs)


# ---- AWS IoT Core MQTT trigger (WebSocket + SigV4, no certs) --------------- #
def _build_iot_request(topic: str, payload: object) -> str:
    """Turn an MQTT message into a natural-language request for the agent.

    Payload routing (dict):
      - {"event": "look", "question": "..."}      -> look via the camera (Cosmos) and answer.
      - {"event": "move", "instruction": "..."}   -> compose a physical gesture (motion tools).
      - {"message": "<sentence>"}                 -> read sentiment, play a matching emotion move.
      - anything else                             -> generic: the agent decides how to react.
    """
    if isinstance(payload, dict):
        event = str(payload.get("event", "")).strip().lower()
        if event in ("look", "look_and_describe", "describe", "vision"):
            return _look_request(str(payload.get("question", "")).strip())
        if event in ("move", "gesture", "motion"):
            instr = str(payload.get("instruction") or payload.get("moves")
                        or payload.get("action") or "").strip()
            return _move_request(instr)
        if str(payload.get("message", "")).strip():
            return _emotion_request(str(payload["message"]).strip())
    body = json.dumps(payload, separators=(",", ":")) if isinstance(payload, (dict, list)) else str(payload)
    return (
        f"An IoT event arrived over MQTT on topic '{topic}' with this payload: {body}. "
        "Decide how to react based on the payload — optionally gesture with a motion (nod, "
        "shake_head, look_around, wiggle_antennas, spin_body, move_head), play an emotion "
        "move, look at the room, or query the IoT datalake — then reply with ONE short "
        "spoken sentence."
    )


def start_iot_listener():
    """Subscribe to the configured MQTT topic; each message enqueues an agent task.

    Returns the live connection (kept referenced so it isn't GC'd) or None when
    the listener is disabled or unavailable — in which case the assistant runs
    voice-only, exactly as before.
    """
    if not (IOT_ENDPOINT and IOT_TOPIC):
        print("[iot] IOT_ENDPOINT/IOT_TOPIC not set — MQTT trigger disabled (voice-only).")
        return None
    try:
        from awscrt import mqtt
        from awscrt.auth import AwsCredentialsProvider
        from awsiot import mqtt_connection_builder
    except Exception as e:  # noqa: BLE001
        print(f"[iot] awsiotsdk not available ({e}) — MQTT trigger disabled.")
        return None

    def on_message(topic, payload, dup, qos, retain, **_kw):  # noqa: ANN001
        raw = bytes(payload).decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001 - plain (non-JSON) payloads pass through as text
            parsed = raw
        print(f"\033[1;34m[iot]\033[0m message on {topic!r}: {raw[:200]}")
        # Fire-and-forget: never run the agent on the MQTT event-loop thread or
        # its keep-alive heartbeats would stall. The worker picks it up.
        req = _build_iot_request(topic, parsed)
        vlog(f"mqtt routed -> enqueue: {req!r}")
        _task_q.put((req, None))

    try:
        conn = mqtt_connection_builder.websockets_with_default_aws_signing(
            endpoint=IOT_ENDPOINT,
            region=IOT_REGION,
            credentials_provider=AwsCredentialsProvider.new_default_chain(),
            client_id=IOT_CLIENT_ID,
            clean_session=True,
            keep_alive_secs=30,
        )
        print(f"[iot] connecting to {IOT_ENDPOINT} (region {IOT_REGION}) as {IOT_CLIENT_ID}...")
        conn.connect().result()
        conn.subscribe(topic=IOT_TOPIC, qos=mqtt.QoS.AT_LEAST_ONCE, callback=on_message)[0].result()
        print(f"\033[1;32m[iot]\033[0m subscribed to {IOT_TOPIC!r} — MQTT events will wake Reachy.")
        # Reuse this same connection to UPLOAD state snapshots on each action.
        set_iot_connection(conn, IOT_STATE_TOPIC or "the-project/reachy-mini/XIAOReachyMini/state")
        return conn
    except Exception as e:  # noqa: BLE001
        print(f"[iot] could not start MQTT listener ({e}) — continuing voice-only.")
        return None


def _start_mic():
    """Open the mic for the voice loop.

    Prefers the media-bus audio broker (a fresh subscription that starts from live
    audio, so the existing terminate()+_start_mic() pattern still drops task-time
    backlog). Falls back to owning the mic directly via arecord when no broker is
    up. Either object exposes .stdout.read(n)/.terminate()/.wait()/.kill().
    """
    if media_bus.broker_available("audio"):
        return media_bus.MicReader()
    return subprocess.Popen(
        ["arecord", "-q", "-D", MIC_DEV, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE,
    )


def main() -> int:
    global mini
    if not os.path.isdir(VOSK_MODEL):
        print(f"[fatal] Vosk model not found at {VOSK_MODEL} (run voice_wake.sh once to fetch it).")
        return 1

    _load_piper()
    try:                                   # warm the move library so its names
        _load_emotions()                   # are in the prompt (one-shot picks)
        print(f"[emotions] loaded {len(EMOTION_NAMES)} pre-choreographed moves.")
    except Exception as e:  # noqa: BLE001
        print(f"[emotions] library unavailable ({e}); play_emotion will report errors.")
    if SESSION_MEMORY:
        print(f"[memory] conversational memory ON — sessions in {SESSION_DIR} "
              f"(rotate after {SESSION_TTL:.0f}s idle, window {SESSION_WINDOW} msgs).")
    else:
        print("[memory] conversational memory OFF (stateless per wake).")
    print("Connecting to Reachy (motors only)...")
    try:
        connection = ReachyMini(media_backend="no_media", connection_mode="localhost_only")
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] could not connect to daemon: {e}")
        return 1

    vmodel = Model(VOSK_MODEL)            # load once
    rec = KaldiRecognizer(vmodel, 16000)

    with connection as r:
        mini = r
        vlog("robot connected (no_media, localhost_only); raising head")
        _head_up()  # raise the head on startup and keep it up for the session

        # One worker owns the robot; voice + MQTT both feed its queue.
        threading.Thread(target=_worker_loop, name="reachy-worker", daemon=True).start()
        vlog("worker thread started")
        iot_conn = start_iot_listener()  # noqa: F841 - kept referenced so it isn't GC'd
        publish_state("startup")  # announce we're online (no-op if MQTT disabled)
        # Keep _latest_frame fresh for head tracking, the Cosmos look path, and the
        # clip recorder. Run the frame subscriber whenever face tracking is on OR a
        # media-bus camera broker is up (the broker owns /dev/video0, so the Cosmos
        # server must be fed image_b64 from here rather than self-capturing).
        _cam_broker = media_bus.broker_available("camera")
        if FACE_TRACK or _cam_broker:
            threading.Thread(target=_capture_loop, name="camera-owner", daemon=True).start()
        if FACE_TRACK:
            threading.Thread(target=_tracker_loop, name="face-tracker", daemon=True).start()
        elif not _cam_broker:
            vlog("face-track off, no broker; Cosmos server will self-capture the camera")
        threading.Thread(target=_idle_watcher, name="idle-watcher", daemon=True).start()

        ar = _start_mic()
        vlog(f"mic capture started ({'media-bus broker' if media_bus.broker_available('audio') else MIC_DEV})")
        print('\nResting. Say "Hey Reachy" (or publish MQTT) to wake me. (Ctrl-C to stop)\n')
        try:
            while True:
                data = ar.stdout.read(4000)
                if not data:
                    print("[warn] mic stream ended."); break
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "")
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "")
                if text and any(tok in text for tok in WAKE_TOKENS):
                    if _busy.is_set():
                        # A task (voice or MQTT) is already running on the worker;
                        # ignore this wake rather than fight it for the robot.
                        vlog(f"wake heard ({text!r}) but busy — ignoring")
                        continue
                    vlog(f"wake word matched in {text!r}; raising head to listen")
                    # head up = "I'm listening" cue, then transcribe the request.
                    mini.enable_motors()
                    mini.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=1.0)
                    request = listen_for_command(ar, vmodel)   # offline, $0
                    vlog(f"transcribed request: {request!r}")
                    if request:
                        # "play emotion ..." routes to the emotion tool; otherwise
                        # the spoken request goes to the generic agent unchanged.
                        task = _route_voice_request(request)
                        vlog(f"voice routed -> enqueue: {task!r}")
                        # Hand to the worker and wait for it so the mic reset
                        # below still drops the backlog captured during the task.
                        done = threading.Event()
                        _task_q.put((task, done))
                        done.wait()
                    else:
                        print("[listen] nothing heard.")
                        speak("I didn't catch that.")
                        _head_up()  # stay up, don't drop to sleep
                    vlog("restarting mic to drop task-time backlog")
                    # restart mic to drop the backlog captured during the task,
                    # and reset the recognizer, before resuming idle listening.
                    ar.terminate()
                    try:
                        ar.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        ar.kill()
                    ar = _start_mic()
                    rec = KaldiRecognizer(vmodel, 16000)
                    print('Resting. Say "Hey Reachy" to wake me.\n')
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            ar.terminate()
            try:
                ar.wait(timeout=2)
            except Exception:  # noqa: BLE001
                ar.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
