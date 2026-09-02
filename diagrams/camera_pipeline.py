"""Single camera owner & concurrency model (feature 11) for the assistant.

Renders docs/camera_pipeline.png — one thread owns /dev/video and publishes the
latest frame; the face tracker, idle watcher, and Cosmos all consume that shared
frame, so head-tracking and vision run together without V4L2 contention. Re-run:

    python3 diagrams/camera_pipeline.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Camera, Nvidia, OpenCV, Python, Reachy
from diagrams import Cluster, Diagram, Edge

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Single camera owner shares frames (face-track + vision, no contention)",
    "pad": "0.4",
    "ranksep": "1.0",
}

with Diagram(
    "camera_pipeline",
    filename="docs/camera_pipeline",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    cam = Camera("/dev/video0\n(V4L2)")

    with Cluster("camera owner thread (_capture_loop)"):
        owner = Python("Reads frames →\n_latest_frame\n(under _cam_lock)")

    with Cluster("consumers (read the shared frame)"):
        tracker = OpenCV("Face tracker\nHaar cascade →\nhead P-controller")
        idle = Python("Idle watcher\nevery IDLE_INTERVAL")
        cosmos = Nvidia("Cosmos Reason 2\nlook_and_describe")

    robot = Reachy("Reachy head\n(follows face)")

    cam >> Edge(label="single reader") >> owner
    owner >> Edge(label="latest frame") >> tracker
    owner >> Edge(label="JPEG b64", style="dashed") >> idle
    owner >> Edge(label="JPEG b64", style="dashed") >> cosmos
    tracker >> Edge(label="yaw/pitch (paused while busy)") >> robot
