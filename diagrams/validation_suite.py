"""Feature 12 — validation suite coverage map.

NOT a data-flow: maps each test/bootstrap script to the component it proves works,
so a failure is isolated to one layer before it's wired into the assistant. Re-run:

    python3 diagrams/validation_suite.py    # -> docs/validation_suite.png
"""

from _icons import Microphone, Nvidia, Ollama, Shell, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.iot import IotCore
from diagrams.aws.ml import Bedrock

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "1.2",
              "label": "Feature 12 · Validation suite — each script proves one layer"}

with Diagram("validation_suite", filename="docs/validation_suite", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    with Cluster("Scripts"):
        s_nem = Shell("nemotron_setup.sh +\ntest_nemotron_agent")
        s_bed = Shell("test_bedrock")
        s_cos = Shell("cosmos_describe.sh +\ntest_cosmos_look")
        s_mqtt = Shell("test_mqtt_*")
        s_wake = Shell("voice_wake")

    with Cluster("Proves"):
        c_nem = Ollama("Ollama / Nemotron\n+ Strands tool-calling")
        c_bed = Bedrock("Bedrock Nova")
        c_cos = Nvidia("Cosmos Reason 2\n(+ warm /look)")
        c_mqtt = IotCore("IoT Core MQTT\ntrigger paths")
        c_wake = Microphone("Vosk offline\nwake word")

    s_nem >> Edge(label="proves") >> c_nem
    s_bed >> Edge(label="proves") >> c_bed
    s_cos >> Edge(label="proves") >> c_cos
    s_mqtt >> Edge(label="proves") >> c_mqtt
    s_wake >> Edge(label="proves") >> c_wake
