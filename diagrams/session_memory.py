"""Feature 17 — conversational memory across wakes.

Renders docs/session_memory.png — each wake builds a FRESH per-wake agent, but a
rotating conversation id (one shared thread for voice + MQTT) lets that agent
reload recent turns from a local JSON session store (Strands FileSessionManager),
bounded by a SlidingWindowConversationManager. The id rotates after SESSION_TTL
idle, starting a new conversation. Fully local ($0), reboot-safe. Re-run:

    python3 diagrams/session_memory.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Person, Python, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.generic.storage import Storage

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "Feature 17 · Conversational memory — fresh agent each wake, recalls recent turns ($0, local)",
    "pad": "0.4",
    "ranksep": "1.0",
}

with Diagram(
    "session_memory",
    filename="docs/session_memory",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    person = Person("voice / MQTT wake\n(any trigger)")

    with Cluster("per-wake handle_wake()"):
        sid = Python("_session_for_now()\nrotating conv id\n(new after SESSION_TTL idle)")
        agent = Strands("FRESH Strands Agent\n+ FileSessionManager\n+ SlidingWindow(SESSION_WINDOW)")

    store = Storage("SESSION_DIR (local JSON)\n~/.cache/reachy_voice/sessions\ndurable · reboot-safe")

    person >> Edge(label="wake") >> sid >> Edge(label="conv id") >> agent
    agent >> Edge(label="persist messages") >> store
    store >> Edge(label="replay recent turns\n(next wake, new Agent)", color="darkgreen", style="dashed") >> agent
    agent >> Edge(label="reply (with context)") >> person
