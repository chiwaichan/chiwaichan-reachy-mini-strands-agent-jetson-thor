"""Feature 3 — offline voice wake-up (voice_wake.py / embedded in the assistant).

Subset: mic audio → arecord PCM → Vosk recognizer → homophone wake-token match.
Fully offline, no LLM. Re-run:

    python3 diagrams/voice_wakeup.py    # -> docs/voice_wakeup.png
"""

from _icons import Microphone, Person, Python, Reachy
from diagrams import Cluster, Diagram, Edge

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "0.9",
              "label": "Feature 3 · Offline wake-up — Vosk, no LLM ($0)"}

with Diagram("voice_wakeup", filename="docs/voice_wakeup", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    person = Person("“Hey Reachy”")
    mic = Microphone("Reachy mic\n(XVF3800)")

    with Cluster("Offline pipeline (CPU, $0)"):
        arecord = Python("arecord\nS16_LE · 16kHz · raw")
        vosk = Microphone("Vosk KaldiRecognizer\nvosk-model-small-en-us")
        match = Python('WAKE_TOKENS match\n("reachy","reach",\n"richie","ritchie"…)')

    head = Reachy("Head raises\n(“I’m listening”)")

    person >> Edge(label="speech") >> mic >> Edge(label="PCM") >> arecord
    arecord >> Edge(label="frames") >> vosk >> Edge(label="partial/final text") >> match
    match >> Edge(label="token found →\nwake", color="darkgreen") >> head
