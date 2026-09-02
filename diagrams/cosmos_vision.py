"""Feature 4 — local vision with Cosmos Reason 2.

Subset: the look_and_describe tool, the warm-server fast path vs. the one-shot
subprocess fallback, sharing one camera. Re-run:

    python3 diagrams/cosmos_vision.py    # -> docs/cosmos_vision.png
"""

from _icons import Camera, Nvidia, Python, Strands
from diagrams import Cluster, Diagram, Edge

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "1.0",
              "label": "Feature 4 · Local vision — Cosmos Reason 2 (warm server + fallback, $0)"}

with Diagram("cosmos_vision", filename="docs/cosmos_vision", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    agent = Strands("Agent →\nlook_and_describe(question)")
    cam = Camera("Camera\n(shared frame or self-capture)")

    with Cluster("Fast path — warm server (model resident)"):
        server = Python("cosmos_server.py\nPOST /look (HTTP)")
        vlm1 = Nvidia("Cosmos Reason 2\n(loaded once, _lock)")

    with Cluster("Fallback — one-shot (server down)"):
        sub = Python("cosmos_describe.py\nsubprocess")
        vlm2 = Nvidia("Cosmos Reason 2\n(cold-load ~5GB)")

    agent >> Edge(label="prefer", color="blue") >> server >> vlm1
    agent >> Edge(label="if /look fails", color="red", style="dashed") >> sub >> vlm2
    cam >> Edge(label="frame / clip") >> server
    cam >> Edge(label="frame / clip") >> sub
    vlm1 >> Edge(label="answer") >> agent
    vlm2 >> Edge(label="answer") >> agent
