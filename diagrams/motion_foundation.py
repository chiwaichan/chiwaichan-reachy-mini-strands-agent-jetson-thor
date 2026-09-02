"""Feature 1 — robot motion foundation (agent_demo.py).

Subset of the architecture: a Bedrock-backed Strands agent calling SDK-wrapping
motion tools that drive the robot through the daemon. Re-run:

    python3 diagrams/motion_foundation.py    # -> docs/motion_foundation.png
"""

from _icons import Person, Python, Reachy, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.ml import Bedrock

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.4", "ranksep": "0.9",
              "label": "Feature 1 · Motion foundation — agent + SDK motion tools"}

with Diagram("motion_foundation", filename="docs/motion_foundation", outformat="png",
             show=False, direction="LR", graph_attr=graph_attr):
    person = Person("Plain-English\ninstruction")
    agent = Strands("agent_demo.py\nStrands Agent")
    bedrock = Bedrock("Bedrock\nNova 2 Lite")

    with Cluster("Motion tools (wrap the Reachy SDK)"):
        tools = Python("wake_up · move_head · nod\nshake_head · look_around\nwiggle_antennas · spin_body\nplay_emotion · rest")

    daemon = Reachy("reachy-mini-daemon")
    robot = Reachy("Reachy Mini\nhead · body · antennas")

    person >> Edge(label="instruction") >> agent
    agent >> Edge(label="reason (capped by\nModelCallBudget)", color="blue", style="dashed") >> bedrock
    agent >> Edge(label="call tools", color="blue") >> tools
    tools >> Edge(label="SDK") >> daemon >> Edge(label="motors") >> robot
    tools >> Edge(label="status string", style="dotted") >> agent
