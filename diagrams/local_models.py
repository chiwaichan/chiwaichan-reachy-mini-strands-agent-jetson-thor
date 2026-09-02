"""Local on-device model stack for the Reachy Mini Lite assistant (localllm branch).

Renders docs/local_models.png — what runs where (venvs / GPU) and how audio and
frames flow through the local models end to end. Re-run after changes:

    python3 diagrams/local_models.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer). Brand icons are
embedded from diagrams/icons/ (see diagrams/_icons.py).
"""

from _icons import Microphone, Nvidia, Ollama, Person, Piper, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.ml import Bedrock

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Local model stack — everything on the Jetson Thor (offline, $0)",
    "pad": "0.4",
    "ranksep": "0.9",
}

with Diagram(
    "local_models",
    filename="docs/local_models",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    person = Person("Person")

    with Cluster(".venv  (assistant — CPU + light)"):
        vosk = Microphone("Vosk STT\nvosk-model-small-en-us-0.15\n~40MB · 16kHz · offline")
        piper = Piper("Piper TTS\nen_US-lessac-medium\n~60MB · neural · offline")
        agent = Strands("Strands Agent\n(per wake)")

    with Cluster("Jetson Thor GPU  (CUDA · fp16 · unified memory)"):
        with Cluster("Ollama runtime"):
            nemotron = Ollama("Nemotron\nnemotron-3-nano:30b\nreasoning + tool-calling")
        with Cluster(".venv-cosmos  (heavy CUDA · cu130)"):
            cosmos = Nvidia("Cosmos Reason 2\nnvidia/Cosmos-Reason2-2B\nQwen3-VL · ~5GB")

    bedrock = Bedrock("Bedrock Nova 2 Lite\n(opt-in cloud swap)")

    person >> Edge(label="“Hey Reachy” + speech") >> vosk
    vosk >> Edge(label="transcribed request") >> agent
    agent >> Edge(label="reason / pick tool\n(every step)", color="blue") >> nemotron
    agent >> Edge(label="look_and_describe", color="blue") >> cosmos
    agent >> Edge(label="LLM_BACKEND=bedrock", style="dashed", color="gray") >> bedrock
    agent >> Edge(label="one short sentence") >> piper
    piper >> Edge(label="speech") >> person
