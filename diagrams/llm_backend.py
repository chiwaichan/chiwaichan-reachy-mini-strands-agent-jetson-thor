"""Feature 6 — pluggable local/cloud LLM brain (_build_model).

Subset: the LLM_BACKEND switch (local Nemotron via Ollama vs. Bedrock Nova), the
ModelCallBudget cap, and the <think> strip before speaking. Re-run:

    python3 diagrams/llm_backend.py    # -> docs/llm_backend.png
"""

from _icons import Ollama, Piper, Python, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.ml import Bedrock

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "0.9",
              "label": "Feature 6 · LLM brain — same agent/tools, swappable backend"}

with Diagram("llm_backend", filename="docs/llm_backend", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    agent = Strands("Strands Agent\n(identical tools + prompt)")
    budget = Python("ModelCallBudget hook\nBeforeModelCallEvent\ncap = MAX_MODEL_CALLS")

    with Cluster("_build_model()  — LLM_BACKEND"):
        ollama = Ollama("ollama (default)\nNemotron · $0 · offline")
        bedrock = Bedrock("bedrock (opt-in)\nNova 2 Lite")

    clean = Python("_clean_reply\nstrip <think>…</think>")
    speak = Piper("speak()")

    agent >> Edge(label="each model call", color="blue") >> budget
    budget >> Edge(label="LLM_BACKEND=ollama", color="darkgreen") >> ollama
    budget >> Edge(label="LLM_BACKEND=bedrock", style="dashed", color="gray") >> bedrock
    ollama >> Edge(label="reply") >> clean
    bedrock >> Edge(label="reply") >> clean
    clean >> speak
