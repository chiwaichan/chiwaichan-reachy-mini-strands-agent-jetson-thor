"""Per-wake agent message flow for the Reachy Mini Lite assistant (localllm branch).

Renders docs/agent_message_flow.png. Re-run after changing the lifecycle:

    python3 diagrams/agent_message_flow.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer). Brand icons are
embedded from diagrams/icons/ (see diagrams/_icons.py).
"""

from _icons import (
    HuggingFace, Iceberg, Nvidia, Ollama, Piper, Python, Strands,
)
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena
from diagrams.aws.compute import Lambda
from diagrams.aws.iot import IotCore

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Per-wake agent message flow (fresh agent → one capped task → destroy)",
    "pad": "0.4",
    "nodesep": "0.6",
    "ranksep": "0.9",
}

with Diagram(
    "agent_message_flow",
    filename="docs/agent_message_flow",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    voice = Python("Voice\n“Hey Reachy” + request")
    mqtt = IotCore("MQTT message\n(IoT Core)")

    with Cluster("1 · Capture & route (offline, $0)"):
        router = Python("Request router\n_route_voice_request /\n_build_iot_request")
        queue = Python("Single-consumer queue\n(one robot owner)")

    with Cluster("2 · Fresh Strands Agent (capped at MAX_MODEL_CALLS)"):
        llm = Ollama("LLM backend\nNemotron (Ollama) | Bedrock")
        agent = Strands("Agent reasons,\npicks ONE tool")

    with Cluster("3 · Tools"):
        look = Nvidia("look_and_describe\n→ Cosmos Reason 2")
        emote = HuggingFace("play_emotion\n→ 1 of ~80 moves")
        with Cluster("datalake (discover → schema → query)"):
            tools_dl = Lambda("list_iot_tables /\nget_table_schema /\nquery_iot_data")
            athena = Athena("Athena")
            s3 = Iceberg("S3 Tables (Iceberg)")

    with Cluster("4 · Respond & tear down"):
        reply = Piper("One short sentence\n→ Piper TTS → speaker")
        teardown = Python("del agent; gc;\nhead stays up → idle")

    voice >> Edge(label="transcribed") >> router
    mqtt >> Edge(color="darkorange") >> router
    router >> queue >> Edge(label="task") >> agent
    agent >> Edge(label="every step", color="blue", style="dashed") >> llm

    agent >> Edge(color="blue") >> look
    agent >> Edge(color="blue") >> emote
    agent >> Edge(color="blue") >> tools_dl
    tools_dl >> Edge(label="SQL") >> athena >> s3

    look >> Edge(label="tool result") >> reply
    emote >> Edge(label="tool result") >> reply
    s3 >> Edge(label="rows") >> reply
    reply >> teardown
