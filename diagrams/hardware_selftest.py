"""Feature 2 — hardware self-test coverage map (hardware_check.py).

NOT a data-flow: a coverage map of which robot subsystems the pure-SDK self-test
exercises (each with a PASS/FAIL/SKIP verdict). Re-run:

    python3 diagrams/hardware_selftest.py    # -> docs/hardware_selftest.png
"""

from _icons import (
    Camera, Doa, HuggingFace, Imu, Microphone, Python, Reachy, Speaker,
)
from diagrams import Cluster, Diagram, Edge

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "1.1",
              "label": "Feature 2 · Hardware self-test coverage (no LLM) — PASS/FAIL/SKIP per subsystem"}

with Diagram("hardware_selftest", filename="docs/hardware_selftest", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    test = Python("hardware_check.py\n(pure SDK, robot moves)")

    with Cluster("Motion"):
        head = Reachy("Head 6-DoF")
        body = Reachy("Body rotation")
        ant = Reachy("Antennas\n(both + each)")
        look = Reachy("look_at")
        emo = HuggingFace("Emotions")

    with Cluster("Sensors"):
        imu = Imu("IMU")
        cam = Camera("Camera")
        doa = Doa("Direction\nof arrival")

    with Cluster("Audio"):
        mic = Microphone("Mic capture")
        spk = Speaker("Speaker")

    for n in (head, body, ant, look, emo, imu, cam, doa, mic, spk):
        test >> Edge(label="verify") >> n
