"""Feature 5 — per-wake assistant lifecycle (reachy_assistant.py).

A state view of the cost-minimal loop: idle (offline) → wake → transcribe → fresh
agent → speak → tear down → idle. Distinct from the message-flow diagram, which
shows tool routing. Re-run:

    python3 diagrams/wake_lifecycle.py    # -> docs/wake_lifecycle.png
"""

from _icons import Microphone, Piper, Python, Reachy, Strands
from diagrams import Diagram, Edge

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "0.8",
              "label": "Feature 5 · Per-wake lifecycle — fresh agent built then destroyed"}

with Diagram("wake_lifecycle", filename="docs/wake_lifecycle", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    idle = Microphone("IDLE\nVosk listening\n(no LLM, $0)")
    wake = Reachy("WAKE\nhead up =\n“listening”")
    listen = Microphone("LISTEN\ntranscribe ONE\nrequest (offline)")
    run = Strands("AGENT\nfresh agent →\nONE capped task")
    speak = Piper("SPEAK\none short\nsentence (TTS)")
    teardown = Python("TEARDOWN\ndel agent; gc\n(head stays up)")

    idle >> Edge(label="wake word / MQTT") >> wake >> Edge(label="raise head") >> listen
    listen >> Edge(label="request") >> run >> Edge(label="result") >> speak
    speak >> Edge(label="done") >> teardown >> Edge(label="back to rest") >> idle
