"""IoT data-lake Q&A flow (feature 7) for the Reachy Mini Lite assistant.

Renders docs/datalake_flow.png — the discover → schema → query pipeline the agent
follows to answer questions about IoT sensor data, and how it resolves and invokes
the Lambdas. Re-run after changes:

    python3 diagrams/datalake_flow.py

Requires Graphviz (`dot`) and the `diagrams` package (mingrammer).
"""

from _icons import Iceberg, Strands
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena
from diagrams.aws.compute import Lambda
from diagrams.aws.management import Cloudformation

graph_attr = {
    "fontsize": "20",
    "labelloc": "t",
    "label": "IoT data-lake Q&A — discover before guess (Lambda + Athena + Iceberg)",
    "pad": "0.4",
    "ranksep": "1.0",
}

with Diagram(
    "datalake_flow",
    filename="docs/datalake_flow",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    agent = Strands("Strands Agent\n(spoken question)")

    with Cluster("Agent tool sequence (system-prompt enforced)"):
        t1 = Lambda("1 · list_iot_tables()\n→ tables + row counts")
        t2 = Lambda("2 · get_table_schema(table)\n→ columns + samples")
        t3 = Lambda("3 · query_iot_data(\ntable, limit, where)")

    cfn = Cloudformation("iot-datalake stack\nTableStats / Query\nFunctionName outputs")

    with Cluster("AWS data lake"):
        stats = Lambda("TableStats\nLambda")
        query = Lambda("Query\nLambda")
        athena = Athena("Athena")
        lake = Iceberg("S3 Tables\n(Apache Iceberg)")

    agent >> Edge(color="blue") >> t1 >> Edge(label="then") >> t2 >> Edge(label="then") >> t3

    # name resolution
    t1 >> Edge(label="resolve name", style="dashed", color="gray") >> cfn
    t3 >> Edge(label="resolve name", style="dashed", color="gray") >> cfn

    # invocation
    t1 >> Edge(label="invoke") >> stats
    t2 >> Edge(label="invoke (limit 1)") >> query
    t3 >> Edge(label="invoke") >> query
    stats >> lake
    query >> Edge(label="SQL") >> athena >> lake

    lake >> Edge(label="rows (JSON)") >> agent
    agent >> Edge(label="ONE spoken sentence\n(never raw JSON)", color="darkgreen")
