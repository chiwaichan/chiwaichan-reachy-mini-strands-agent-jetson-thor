"""Feature 10 — idle human-detection watcher.

Subset: a timer grabs one frame → Cosmos observation → minimal Strands agent →
report_presence tool (logs for now). Skipped while a task runs. Re-run:

    python3 diagrams/idle_watcher.py    # -> docs/idle_watcher.png
"""

from _icons import Camera, Nvidia, Python, Strands
from diagrams import Cluster, Diagram, Edge

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "0.9",
              "label": "Feature 10 · Idle watcher — periodic presence check ($0, local)"}

with Diagram("idle_watcher", filename="docs/idle_watcher", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    timer = Python("_idle_watcher loop\nevery IDLE_INTERVAL\n(skip if _busy)")
    cam = Camera("single frame\n(shared camera)")
    cosmos = Nvidia("Cosmos Reason 2\nIDLE_QUESTION")

    with Cluster("Minimal Strands agent"):
        agent = Strands("presence agent\n(human / cat router)")
        tool = Python("report_human_presence /\nreport_cat_presence\n→ clip + state (feature 13)")

    timer >> Edge(label="tick") >> cam >> Edge(label="image") >> cosmos
    cosmos >> Edge(label="observation") >> agent
    agent >> Edge(label="if person/cat seen", color="blue") >> tool
