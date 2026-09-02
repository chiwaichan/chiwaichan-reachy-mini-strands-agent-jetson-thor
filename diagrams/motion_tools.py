"""Feature 18 — direct motion tools (compose novel gestures).

Renders docs/motion_tools.png — six primitive motion tools (promoted from
agent_demo.py) are given to the per-wake agent so it can compose gestures beyond
the canned play_emotion clips. Reachable from voice passthrough and the MQTT
{"event":"move","instruction":"..."} route. Each clamps its inputs, drives the
Reachy SDK, and publishes a 'motion' state snapshot. Safe mid-task: the face
tracker is paused while _busy is set. Re-run:

    python3 diagrams/motion_tools.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Person, Python, Reachy, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Feature 18 · Direct motion tools — compose gestures (nod, look_around, spin_body, …)",
    "pad": "0.4",
    "ranksep": "1.0",
}

with Diagram(
    "motion_tools",
    filename="docs/motion_tools",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    with Cluster("triggers"):
        voice = Person('voice passthrough\n"nod twice, then spin"')
        mqtt = IotCore('MQTT {"event":"move",\n"instruction":"..."}')

    agent = Strands("Per-wake Strands Agent\none tool per step, in order")

    with Cluster("motion tools (clamped, return to neutral)"):
        tools = Python("nod · shake_head · look_around\nwiggle_antennas · spin_body · move_head")

    robot = Reachy("Reachy SDK goto_target\nhead · body · antennas")
    iot = IotCore("publish_state('motion')\nmotion + params")

    voice >> Edge(label="request") >> agent
    mqtt >> Edge(label="_move_request()") >> agent
    agent >> Edge(label="compose steps", color="blue") >> tools
    tools >> Edge(label="goto_target") >> robot
    tools >> Edge(label="telemetry", style="dashed", color="gray") >> iot
