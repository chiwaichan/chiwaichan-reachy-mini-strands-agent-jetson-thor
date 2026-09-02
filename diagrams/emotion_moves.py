"""Feature 9 — emotion moves (sentiment → one pre-choreographed move).

Subset: the two triggers (voice prefix / MQTT message) → sentiment → play_emotion
→ Hugging Face move library → daemon → robot. Re-run:

    python3 diagrams/emotion_moves.py    # -> docs/emotion_moves.png
"""

from _icons import HuggingFace, Microphone, Python, Reachy, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "0.9",
              "label": "Feature 9 · Emotion moves — sentiment → 1 of ~80 moves"}

with Diagram("emotion_moves", filename="docs/emotion_moves", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    with Cluster("Triggers"):
        voice = Microphone('Voice prefix\n"play emotion, …"')
        mqtt = IotCore('MQTT {"message": …}')

    route = Python("_emotion_request\n(wrap sentiment)")
    agent = Strands("Agent picks ONE\nmove name")
    lib = HuggingFace("reachy-mini-\nemotions-library\n(names in prompt)")
    daemon = Reachy("play_move → daemon")
    robot = Reachy("Reachy performs\nthe move")

    voice >> route
    mqtt >> Edge(color="darkorange") >> route
    route >> Edge(label="sentiment") >> agent
    agent >> Edge(label="validate name", style="dashed") >> lib
    agent >> Edge(label="play_emotion(name)", color="blue") >> daemon >> robot
