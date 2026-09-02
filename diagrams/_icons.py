"""Shared custom-icon node factories for the project diagrams.

Brand/component icons that have no accurate built-in `diagrams` node are embedded
from real logos in ``diagrams/icons/`` (downloaded from each project's official
source). AWS services keep the official `diagrams.aws.*` nodes.

Icon provenance (all fetched from the canonical source):
  nvidia      simpleicons.org (NVIDIA brand mark, #76B900)
  ollama      ollama.com/public/ollama.png
  strands     github.com/strands-agents (org logo)
  reachy      github.com/pollen-robotics (org logo)
  huggingface huggingface.co brand logo
  piper       github.com/OHF-Voice/piper1-gpl
  python      devicons/devicon
  opencv      devicons/devicon
  iceberg     apache/iceberg
  microphone/camera/speaker  tabler-icons (neutral device glyphs)

Paths are resolved relative to this file, so the diagrams render correctly no
matter the current working directory.
"""

import os

from diagrams.custom import Custom

_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def _icon(name: str) -> str:
    return os.path.join(_ICON_DIR, name)


def Nvidia(label: str) -> Custom:
    return Custom(label, _icon("nvidia.png"))


def Ollama(label: str) -> Custom:
    return Custom(label, _icon("ollama.png"))


def Strands(label: str) -> Custom:
    return Custom(label, _icon("strands.png"))


def Reachy(label: str) -> Custom:
    return Custom(label, _icon("reachy.png"))


def Person(label: str) -> Custom:
    return Custom(label, _icon("user.png"))


def HuggingFace(label: str) -> Custom:
    return Custom(label, _icon("huggingface.png"))


def Piper(label: str) -> Custom:
    return Custom(label, _icon("piper.png"))


def Python(label: str) -> Custom:
    return Custom(label, _icon("python.png"))


def OpenCV(label: str) -> Custom:
    return Custom(label, _icon("opencv.png"))


def Iceberg(label: str) -> Custom:
    return Custom(label, _icon("iceberg.png"))


def Microphone(label: str) -> Custom:
    return Custom(label, _icon("microphone.png"))


def Camera(label: str) -> Custom:
    return Custom(label, _icon("camera.png"))


def Speaker(label: str) -> Custom:
    return Custom(label, _icon("speaker.png"))


def Shell(label: str) -> Custom:
    return Custom(label, _icon("terminal.png"))


def Imu(label: str) -> Custom:
    return Custom(label, _icon("cpu.png"))


def Doa(label: str) -> Custom:
    return Custom(label, _icon("compass.png"))


def Imed(label: str) -> Custom:  # generic gauge / measurement
    return Custom(label, _icon("gauge.png"))


def Queue(label: str) -> Custom:  # bounded per-subscriber queue / socket fan-out
    return Custom(label, _icon("queue.png"))
