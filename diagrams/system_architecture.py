"""System architecture diagram for the Reachy Mini Lite assistant (localllm branch).

Renders docs/system_architecture.png. Re-run after changing the topology:

    python3 diagrams/system_architecture.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer). Brand icons are
embedded from diagrams/icons/ (see diagrams/_icons.py).
"""

from _icons import (
    Camera, HuggingFace, Iceberg, Microphone, Nvidia, Ollama, OpenCV,
    Person, Piper, Python, Reachy, Speaker, Strands,
)
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena
from diagrams.aws.compute import Lambda
from diagrams.aws.iot import IotCore
from diagrams.aws.ml import Bedrock
from diagrams.aws.storage import S3

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Reachy Mini Lite × Strands — local-first voice & vision assistant",
    "pad": "0.4",
    "splines": "spline",
}

with Diagram(
    "system_architecture",
    filename="docs/system_architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    person = Person("Person\n(voice / face)")

    with Cluster("Reachy Mini Lite (USB)"):
        robot = Reachy("Robot\nhead · antennas · body")
        mic = Microphone("Mic (XVF3800)")
        cam = Camera("Camera")
        spk = Speaker("Speaker")

    with Cluster("Jetson Thor — edge host (offline, $0)"):
        mediabus = Python("media_bus.py\n1 owner per device →\nfan out (Unix socket)")

        with Cluster("reachy_assistant.py (orchestrator)"):
            vosk = Microphone("Vosk wake-word\n+ STT (offline)")
            worker = Python("Single robot worker\n(request queue)")
            agent = Strands("Per-wake Strands Agent\n(built → run → destroyed)")
            face = OpenCV("Face tracker\n(Haar cascade)")
            idle = Python("Idle presence watcher\n(humans vs cats)")

        ollama = Ollama("Ollama → Nemotron\n(local LLM, default)")
        cosmos = Nvidia("cosmos_server.py\nCosmos Reason 2 VLM")
        piper = Piper("Piper / espeak-ng\nTTS")
        moves = HuggingFace("Emotion moves\nlibrary (~80)")

        daemon = Reachy("reachy-mini-daemon\n--no-media")

    with Cluster("AWS (optional cloud paths)"):
        iot = IotCore("IoT Core\ntrigger + state telemetry")
        clips = S3("S3\ninteraction clips\n+ presigned URL")
        with Cluster("IoT datalake"):
            lam = Lambda("TableStats /\nQuery Lambdas")
            athena = Athena("Athena")
            s3 = Iceberg("S3 Tables\n(Apache Iceberg)")
        bedrock = Bedrock("Bedrock\nNova 2 Lite (opt-in)")

    # --- wake & capture (offline; devices owned by the media bus) ---
    person >> Edge(label="“Hey Reachy”") >> mic
    person >> Edge(style="dashed") >> cam
    mic >> Edge(label="audio") >> mediabus >> Edge(label="PCM") >> vosk
    vosk >> Edge(label="request") >> worker

    # --- MQTT trigger (2nd wake source) ---
    iot >> Edge(label="message", color="darkorange") >> worker

    # --- per-wake task ---
    worker >> Edge(label="task") >> agent

    # --- agent tool calls ---
    agent >> Edge(label="reason / tool-pick", color="blue") >> ollama
    agent >> Edge(label="look_and_describe", color="blue") >> cosmos
    agent >> Edge(label="play_emotion", color="blue") >> moves >> daemon
    agent >> Edge(label="datalake tools", color="blue") >> lam
    agent >> Edge(label="LLM_BACKEND=bedrock", style="dashed", color="gray") >> bedrock

    # --- datalake resolution ---
    lam >> Edge(label="SQL") >> athena >> s3

    # --- vision & motion I/O (frames fanned out by the media bus) ---
    cam >> Edge(label="frames") >> mediabus
    mediabus >> Edge(label="JPEG") >> face
    mediabus >> Edge(label="JPEG") >> cosmos
    mediabus >> Edge(label="JPEG") >> idle
    idle >> Edge(label="presence → agent", style="dotted") >> agent
    face >> Edge(label="track head") >> daemon
    daemon >> Edge(label="motors") >> robot

    # --- cloud side-effects (non-blocking, no-op when off) ---
    agent >> Edge(label="state telemetry", color="darkorange", style="dashed") >> iot
    agent >> Edge(label="clip upload", color="darkgreen", style="dashed") >> clips

    # --- spoken reply ---
    agent >> Edge(label="reply text") >> piper >> Edge(label="audio") >> spk
