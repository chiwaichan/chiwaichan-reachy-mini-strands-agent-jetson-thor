"""Strands agent that drives a Reachy Mini Lite — a "see it in action" demo.

The agent is given a set of motion tools that wrap the Reachy Mini Python SDK.
It uses Amazon Bedrock (default AWS profile/credentials) to decide which motions
to perform in response to a natural-language instruction, then executes them on
the physical robot over USB via the local daemon.

Run it through ``run.sh`` (which installs deps + starts the daemon), or directly:

    python agent_demo.py "nod twice, then look around the room"

Configuration (environment variables):
    AWS_REGION         AWS region for Bedrock        (default: us-east-1)
    BEDROCK_MODEL_ID   Bedrock model / inference id  (default: Amazon Nova 2 Lite)
    MEDIA_BACKEND      reachy media backend          (default: "default";
                       set "no_media" to skip camera/mic/speaker entirely)
"""

from __future__ import annotations

import logging
import os
import sys

import numpy as np
from strands import Agent, tool
from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel

from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils import create_head_pose

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reachy_strands_demo")

# Single shared connection to the robot, assigned in main() before the agent runs.
mini: ReachyMini | None = None
# Pre-recorded emotion animations from Hugging Face, loaded lazily on first use.
_emotions = None


class ModelCallBudget(HookProvider):
    """Hard ceiling on Bedrock model calls per agent invocation.

    Each agent-loop turn makes one model call; this aborts the run once the cap is
    hit so a runaway loop can never rack up unbounded Bedrock cost. Override the
    ceiling with the MAX_MODEL_CALLS env var.
    """

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.count = 0

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)

    def _on_before_model_call(self, _event: BeforeModelCallEvent) -> None:
        self.count += 1
        if self.count > self.max_calls:
            raise RuntimeError(
                f"Model-call budget exceeded ({self.max_calls}) — aborting to "
                f"prevent runaway Bedrock cost."
            )
        logger.info("Bedrock model call %d/%d", self.count, self.max_calls)


def _robot() -> ReachyMini:
    if mini is None:
        raise RuntimeError("Robot is not connected yet.")
    return mini


def _load_emotions():
    global _emotions
    if _emotions is None:
        from reachy_mini.motion.recorded_move import RecordedMoves

        _emotions = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
    return _emotions


# --------------------------------------------------------------------------- #
# Motion tools — each wraps the Reachy SDK and returns a short status string   #
# that the agent reads back as the tool result.                               #
# --------------------------------------------------------------------------- #
@tool
def wake_up() -> str:
    """Wake the robot: enable the motors and move to the upright neutral pose."""
    r = _robot()
    r.enable_motors()
    r.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=2.0)
    return "Awake and centered."


@tool
def move_head(pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0, duration: float = 1.0) -> str:
    """Move the head to an orientation given in degrees.

    Args:
        pitch: Up/down tilt (negative looks up, positive looks down). Range ~[-40, 40].
        roll: Sideways tilt of the head. Range ~[-40, 40].
        yaw: Left/right turn. Range ~[-180, 180].
        duration: Seconds for the smooth movement.
    """
    r = _robot()
    pose = create_head_pose(roll=roll, pitch=pitch, yaw=yaw, degrees=True)
    r.goto_target(pose, duration=max(0.3, duration))
    return f"Head moved to pitch={pitch}, roll={roll}, yaw={yaw} degrees."


@tool
def nod(times: int = 2) -> str:
    """Nod the head up and down to say 'yes'."""
    r = _robot()
    for _ in range(max(1, min(times, 5))):
        r.goto_target(create_head_pose(pitch=15, degrees=True), duration=0.35)
        r.goto_target(create_head_pose(pitch=-10, degrees=True), duration=0.35)
    r.goto_target(INIT_HEAD_POSE, duration=0.35)
    return f"Nodded {times} time(s)."


@tool
def shake_head(times: int = 2) -> str:
    """Shake the head left and right to say 'no'."""
    r = _robot()
    for _ in range(max(1, min(times, 5))):
        r.goto_target(create_head_pose(yaw=25, degrees=True), duration=0.35)
        r.goto_target(create_head_pose(yaw=-25, degrees=True), duration=0.35)
    r.goto_target(INIT_HEAD_POSE, duration=0.35)
    return f"Shook head {times} time(s)."


@tool
def look_around() -> str:
    """Sweep the head left, right, and back to center to scan the room."""
    r = _robot()
    r.goto_target(create_head_pose(yaw=60, degrees=True), duration=1.0)
    r.goto_target(create_head_pose(yaw=-60, degrees=True), duration=1.5)
    r.goto_target(INIT_HEAD_POSE, duration=1.0)
    return "Looked around the room."


@tool
def wiggle_antennas(times: int = 3) -> str:
    """Wiggle both antennas up and down expressively."""
    r = _robot()
    up = [0.5, 0.5]  # radians
    down = [-0.5, -0.5]
    for _ in range(max(1, min(times, 6))):
        r.goto_target(antennas=up, duration=0.25)
        r.goto_target(antennas=down, duration=0.25)
    r.goto_target(antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=0.25)
    return f"Wiggled antennas {times} time(s)."


@tool
def spin_body(degrees: float = 90.0, duration: float = 1.5) -> str:
    """Rotate the body around its vertical axis by the given angle in degrees (range ~[-160, 160])."""
    r = _robot()
    r.goto_target(body_yaw=float(np.deg2rad(degrees)), duration=max(0.3, duration))
    return f"Rotated body to {degrees} degrees."


@tool
def list_emotions() -> str:
    """List the names of the pre-recorded emotion animations available to play."""
    try:
        return ", ".join(_load_emotions().list_moves())
    except Exception as e:  # noqa: BLE001 - report instead of crashing the agent
        return f"Emotion library unavailable: {e}"


@tool
def play_emotion(name: str) -> str:
    """Play a pre-recorded expressive emotion animation by name (see list_emotions).

    Args:
        name: The emotion/animation name, e.g. "happy", "curious", "sad".
    """
    r = _robot()
    try:
        move = _load_emotions().get(name)
        r.play_move(move, initial_goto_duration=1.0, sound=False)
        return f"Played emotion '{name}'."
    except Exception as e:  # noqa: BLE001
        return f"Could not play '{name}': {e}"


@tool
def rest() -> str:
    """Return the head and antennas to the neutral pose. Call this to finish a routine."""
    r = _robot()
    r.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=1.5)
    return "Back to neutral."


TOOLS = [
    wake_up,
    move_head,
    nod,
    shake_head,
    look_around,
    wiggle_antennas,
    spin_body,
    list_emotions,
    play_emotion,
    rest,
]

SYSTEM_PROMPT = """You are the mind of a Reachy Mini Lite, a small expressive desktop robot.
You have a physical body you control through tools: a 6-DOF head, a body that rotates around a
vertical axis, and two antennas. When the user asks you to do something, EXPRESS yourself
physically by calling your motion tools — chain several of them together to make the behavior
lively and natural rather than doing the bare minimum. Keep any spoken text short; the movement
is the point. Begin a fresh routine with wake_up if you have not moved yet, and always finish by
calling rest to return to a neutral pose."""

DEFAULT_INSTRUCTION = (
    "Introduce yourself to me: wake up, look around the room to see who's here, "
    "then show me you're happy and excited to meet me. Be expressive!"
)


def main() -> int:
    global mini

    region = os.environ.get("AWS_REGION", "us-east-1")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
    media_backend = os.environ.get("MEDIA_BACKEND", "default")
    max_model_calls = int(os.environ.get("MAX_MODEL_CALLS", "15"))
    instruction = " ".join(sys.argv[1:]).strip() or DEFAULT_INSTRUCTION

    logger.info("Connecting to Reachy Mini (media_backend=%s)...", media_backend)
    try:
        connection = ReachyMini(media_backend=media_backend)
    except Exception as e:  # noqa: BLE001
        logger.error("Could not connect to the robot: %s", e)
        logger.error("Is the daemon running (run.sh starts it) and the robot plugged in via USB?")
        return 1

    with connection as r:
        mini = r
        logger.info("Building Strands agent on Bedrock model '%s' (%s)...", model_id, region)
        agent = Agent(
            model=BedrockModel(model_id=model_id, region_name=region),
            tools=TOOLS,
            system_prompt=SYSTEM_PROMPT,
            hooks=[ModelCallBudget(max_model_calls)],
        )
        logger.info("Bedrock model-call budget: %d (override via MAX_MODEL_CALLS)", max_model_calls)

        logger.info("Instruction: %s", instruction)
        exit_code = 0
        try:
            agent(instruction)
        except RuntimeError as e:
            # Model-call budget tripped (or similar guard) — abort cleanly, no traceback.
            logger.error("Run aborted: %s", e)
            exit_code = 3
        finally:
            # Always leave the robot in a safe neutral pose, even on error/Ctrl-C.
            try:
                r.goto_target(
                    INIT_HEAD_POSE,
                    antennas=INIT_ANTENNAS_JOINT_POSITIONS,
                    duration=1.5,
                )
            except Exception:  # noqa: BLE001
                pass

    print("\n--- Agent finished ---")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
