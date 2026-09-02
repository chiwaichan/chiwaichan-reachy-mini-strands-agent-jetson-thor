"""Feature 13 — idle presence split into human vs cat detection tools.

Renders docs/presence_split.png — the idle Cosmos observation is handed to a
minimal Strands agent that now routes to TWO species-specific tools:
report_human_presence and report_cat_presence (both, either, or neither). Each
tool, when its subject is seen, records a short clip and publishes a presence
state message. Re-run after changes:

    python3 diagrams/presence_split.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Camera, Nvidia, Python, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore
from diagrams.aws.storage import S3

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Feature 13 · Idle presence split — humans vs cats (one observation, two tools)",
    "pad": "0.4",
    "ranksep": "0.9",
}

with Diagram(
    "presence_split",
    filename="docs/presence_split",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    cam = Camera("single frame\n(shared camera)")
    cosmos = Nvidia("Cosmos Reason 2\nIDLE_QUESTION\n(people + cats)")

    with Cluster("Minimal Strands agent (router)"):
        agent = Strands("presence agent\n(both / either / neither)")
        human = Python("report_human_presence(\npeople, description)")
        cat = Python("report_cat_presence(\ncats, description)")

    with Cluster("on detection (count >= 1)"):
        clip = S3("record_clip_and_upload()\n-> presigned URL")
        iot = IotCore("publish_state('presence')\npresence_kind=human|cat")

    cam >> Edge(label="image") >> cosmos >> Edge(label="observation") >> agent
    agent >> Edge(label="if PEOPLE seen", color="magenta") >> human
    agent >> Edge(label="if CATS seen", color="blue") >> cat
    human >> Edge(label="count>=1") >> clip
    cat >> Edge(label="count>=1") >> clip
    human >> Edge(style="dashed", color="gray") >> iot
    cat >> Edge(style="dashed", color="gray") >> iot
