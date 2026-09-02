"""Feature 15 — interaction clip recording -> S3 -> presigned URL.

Renders docs/clip_recording.png — during a wake interaction (or a presence
event) frames are SAMPLED from the shared camera buffer (no 2nd VideoCapture,
which would collide with the camera owner), encoded to MP4 with cv2.VideoWriter
(mp4v), uploaded to the S3 bucket, and a presigned GET URL is attached to the
"reply" MQTT state message. Re-run after changes:

    python3 diagrams/clip_recording.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Camera, OpenCV, Python
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore
from diagrams.aws.storage import S3

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Feature 15 · Interaction clip — sample shared camera -> MP4 -> S3 -> presigned URL",
    "pad": "0.4",
    "ranksep": "1.4",
    "nodesep": "0.8",
}

with Diagram(
    "clip_recording",
    filename="docs/clip_recording",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    buf = Camera("_latest_frame\n(shared camera buffer)")

    with Cluster("start_recording() -> capture thread"):
        rec = Python("_record_loop\nsample @ VIDEO_FPS\ncap VIDEO_MAX_SECONDS")

    with Cluster("stop_recording_and_upload()"):
        enc = OpenCV("cv2.VideoWriter\n(mp4v) -> .mp4")
        up = S3("boto3 upload_file\nvideos/reachy_*.mp4")
        url = Python("generate_presigned_url\n(GET,\nPRESIGNED_URL_EXPIRY)")

    bucket = S3("S3_BUCKET\n(your bucket)")
    reply = IotCore("reply message\nvideo_url ->\nthe-project/reachy-mini/\nXIAOReachyMini/state")

    buf >> Edge(label="frame.copy()") >> rec
    rec >> Edge(label="frames[]") >> enc >> Edge(label="tmp .mp4") >> up
    up >> Edge(label="put object") >> bucket
    up >> Edge(label="then") >> url
    url >> Edge(label="presigned URL", color="darkgreen") >> reply
