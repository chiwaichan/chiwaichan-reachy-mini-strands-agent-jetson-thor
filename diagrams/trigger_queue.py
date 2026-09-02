"""Dual-trigger single-owner queue (feature 8) for the Reachy Mini Lite assistant.

Renders docs/trigger_queue.png — how the voice wake-word loop and the MQTT listener
both enqueue onto one queue drained by a single worker, so the robot is never driven
by two sources at once. Re-run after changes:

    python3 diagrams/trigger_queue.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Microphone, Python, Reachy, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Two trigger sources, one robot owner (single-consumer queue)",
    "pad": "0.4",
    "ranksep": "1.0",
}

with Diagram(
    "trigger_queue",
    filename="docs/trigger_queue",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    with Cluster("Trigger sources (concurrent)"):
        wake = Microphone("Wake-word loop\nVosk “Hey Reachy”\n(main thread)")
        mqtt = IotCore("MQTT listener\nIoT Core (SigV4)\n(fire-and-forget)")

    queue = Python("_task_q\nqueue.Queue\n(request, done?)")

    with Cluster("Single worker thread (owns the robot)"):
        worker = Python("_worker_loop\n_busy set while running")
        agent = Strands("handle_wake →\nfresh Strands Agent")

    robot = Reachy("Reachy Mini\nmotors · camera · speaker")

    wake >> Edge(label="enqueue (waits on done)") >> queue
    mqtt >> Edge(label="enqueue (no wait)", color="darkorange") >> queue
    queue >> Edge(label="one at a time") >> worker >> agent >> Edge(label="drives") >> robot

    # back-pressure / mutual exclusion
    worker >> Edge(label="wake heard while _busy → ignored", style="dashed", color="red") >> wake
