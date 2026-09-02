"""Feature 16 — camera/mic media bus: one owner per device, fan out to many.

Renders docs/media_bus.png — media_bus.py runs ONE broker that owns /dev/video0
(MJPG) and ONE that owns the mic (arecord S16LE), and republishes each live
stream over a Unix domain socket. Every consumer gets its own bounded queue +
sender thread, so a slow/crashed subscriber drops only its OWN frames and never
stalls the device loop or its peers. The assistant subscribes for face tracking,
the idle watcher and clip recording; any new process (and the poc_fanout/ procs)
can subscribe to the same live devices at once. Re-run after changes:

    python3 diagrams/media_bus.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Camera, Microphone, Nvidia, OpenCV, Python, Queue, Strands
from diagrams import Cluster, Diagram, Edge

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Feature 16 · Media bus — one owner per device, fan out to many processes (no V4L2/ALSA contention)",
    "pad": "0.4",
    "ranksep": "1.1",
}

with Diagram(
    "media_bus",
    filename="docs/media_bus",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    cam = Camera("/dev/video0\n(V4L2, single opener)")
    mic = Microphone("mic\n(ALSA, single opener)")

    with Cluster("media_bus.py brokers (one process each)"):
        cam_brk = Python("camera broker\nMJPG ~30fps\n-> JPEG on /tmp/reachy_cam.sock")
        aud_brk = Python("audio broker\narecord S16LE/16k/mono\n-> PCM on /tmp/reachy_audio.sock")
        with Cluster("per-subscriber bounded queue + sender thread"):
            q = Queue("qdepth=3\nslow consumer drops\nonly its own frames")

    with Cluster("assistant consumers (reachy_assistant.py)"):
        track = OpenCV("face tracker\n(_capture_loop)")
        idle = Python("idle watcher\n+ clip recorder")
        cosmos = Nvidia("Cosmos look\n(image_b64)")
        mic_rd = Strands("MicReader\n(voice loop)")

    with Cluster("any other process (poc_fanout/)"):
        ext = Python("late join · crash-isolated\nvision / motion / audio subs")

    cam >> Edge(label="single reader") >> cam_brk
    mic >> Edge(label="single reader") >> aud_brk
    cam_brk >> q
    aud_brk >> q
    q >> Edge(label="frames") >> track
    q >> Edge(label="frames") >> idle
    q >> Edge(label="frames") >> cosmos
    q >> Edge(label="PCM") >> mic_rd
    q >> Edge(label="Unix socket\n(subscribe)", style="dashed", color="blue") >> ext
