"""Feature 14 — robot-state telemetry uploaded to AWS IoT Core.

Renders docs/iot_state.png — every agent action calls publish_state(), which
snapshots the full Reachy Mini state (9 servo joints, head pose, daemon status,
runtime flags) via the SDK, then ships it as JSON to the the-project/reachy-mini/XIAOReachyMini/state MQTT
topic on a single background thread (the same IoT Core connection the trigger
already holds). Re-run after changes:

    python3 diagrams/iot_state.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Reachy, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore
from diagrams.onprem.compute import Server

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Feature 14 · Robot-state telemetry — full snapshot to IoT Core on each action",
    "pad": "0.4",
    "ranksep": "1.0",
}

with Diagram(
    "iot_state",
    filename="docs/iot_state",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    with Cluster("triggers (each agent action)"):
        actions = Strands("startup · emotion\nvision · presence · reply")

    with Cluster("publish_state(trigger, **fields)"):
        snap = Server("_read_robot_state()\nservos(9) · head_pose\ndaemon · runtime")
        pool = Server("ThreadPoolExecutor\n(max_workers=1)\nnon-blocking")

    robot = Reachy("Reachy Mini SDK\nget_current_joint_positions()\nget_current_head_pose()\nclient.get_status()")
    topic = IotCore("AWS IoT Core\nthe-project/reachy-mini/XIAOReachyMini/state\n(IOT_STATE_TOPIC)")

    actions >> Edge(label="call") >> snap
    snap >> Edge(label="read", style="dashed", color="gray") >> robot
    snap >> Edge(label="JSON + ctx fields") >> pool
    pool >> Edge(label="QoS 1 publish\n(reuses trigger conn)", color="darkgreen") >> topic
