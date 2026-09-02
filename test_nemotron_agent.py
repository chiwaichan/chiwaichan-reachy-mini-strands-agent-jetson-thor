#!/usr/bin/env python3
"""Prove local Nemotron (via Ollama) can drive the EXACT Reachy agent tool surface
before we swap Nova 2 Lite for it.

The tools below mirror reachy_assistant.py's tools 1:1 — same names, parameters,
docstrings, and the same system prompt — but with safe stub bodies (no robot, no
AWS, no Cosmos). Each stub records its call + args, so we verify Nemotron:

  1. calls look_and_describe (vision) and passes a question,
  2. calls play_emotion with a VALID move name from the 80-move library,
  3. runs the multi-step datalake workflow: list_iot_tables -> (schema) -> query.

  ./test_nemotron_agent.sh
  python test_nemotron_agent.py
"""

from __future__ import annotations

import json
import os

from strands import Agent, tool
from strands.models.ollama import OllamaModel

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL_ID = os.environ.get("NEMOTRON_MODEL", "nemotron-3-nano:30b")

# (name, args) for every tool call in the current case — our proof of real tool use.
CALLS: list[tuple[str, dict]] = []

# The 80 pre-choreographed move names (mirrors reachy's emotion library).
EMOTION_NAMES = [
    "anxiety1", "attentive1", "attentive2", "boredom1", "boredom2", "calming1", "cheerful1", "come1",
    "confused1", "contempt1", "curious1", "dance1", "dance2", "dance3", "disgusted1", "displeased1",
    "displeased2", "downcast1", "dying1", "electric1", "enthusiastic1", "enthusiastic2", "exhausted1",
    "fear1", "frustrated1", "furious1", "go_away1", "grateful1", "helpful1", "helpful2", "impatient1",
    "impatient2", "incomprehensible2", "indifferent1", "inquiring1", "inquiring2", "inquiring3",
    "irritated1", "irritated2", "laughing1", "laughing2", "lonely1", "lost1", "loving1", "no1",
    "no_excited1", "no_sad1", "oops1", "oops2", "proud1", "proud2", "proud3", "rage1", "relief1",
    "relief2", "reprimand1", "reprimand2", "reprimand3", "resigned1", "sad1", "sad2", "scared1",
    "serenity1", "shy1", "sleep1", "success1", "success2", "surprised1", "surprised2", "thoughtful1",
    "thoughtful2", "tired1", "uncertain1", "uncomfortable1", "understanding1", "understanding2",
    "welcoming1", "welcoming2", "yes1", "yes_sad1",
]

# Fake datalake so the multi-step workflow can actually complete.
_TABLES = {
    "water_leak_detector": {"water_detected": "true", "device_id": "wl-01", "location": "kitchen", "ts": "2026-06-23T10:00:00Z"},
    "presence": {"motion_detected": "true", "device_id": "pir-02", "room": "office", "ts": "2026-06-23T09:55:00Z"},
    "environment_monitor": {"temperature_c": "21.4", "humidity": "47", "device_id": "env-03", "ts": "2026-06-23T09:50:00Z"},
}


# ─── Tools: mirror reachy_assistant.py (stubbed, call-recording) ───────────── #
@tool
def look_and_describe(question: str = "") -> str:
    """Look through the robot's camera and answer a question about what is seen.

    Use this whenever the request needs the robot's eyes — to see the room, find
    or identify something, read visible text, count things, or check what a person
    is doing.

    Args:
        question: What to look for or answer about the scene (e.g. "what color is
            the mug?"). Leave empty for a general description of the scene.
    """
    CALLS.append(("look_and_describe", {"question": question}))
    return "I can see one person sitting at a desk with two monitors and a coffee mug."


@tool
def list_emotion_moves() -> str:
    """List the names of the pre-choreographed emotion moves available to play."""
    CALLS.append(("list_emotion_moves", {}))
    return ", ".join(EMOTION_NAMES)


@tool
def play_emotion(name: str) -> str:
    """Play ONE pre-choreographed emotion move on the robot by name.

    Pick the single move whose mood best matches the request or message sentiment
    (e.g. 'success1', 'proud1', 'sad1', 'welcoming1', 'laughing1', 'curious1').

    Args:
        name: An exact move name from list_emotion_moves. Do not invent names.
    """
    CALLS.append(("play_emotion", {"name": name}))
    if name not in EMOTION_NAMES:
        return f"'{name}' is not a valid move. Choose one of: {', '.join(EMOTION_NAMES)}"
    return f"Played '{name}'."


@tool
def list_iot_tables() -> str:
    """List all available IoT device tables with row counts and last ingestion times.

    Use this FIRST to discover what tables exist before querying them.
    """
    CALLS.append(("list_iot_tables", {}))
    return json.dumps({
        "tables": [{"name": t, "rows": 1000 + i} for i, t in enumerate(_TABLES)],
        "total_rows": 3003,
    }, indent=2)


@tool
def get_table_schema(table: str) -> str:
    """Get the column names and sample values for a specific IoT table.

    Use this to discover available columns before querying with WHERE filters.

    Args:
        table: The table name (e.g. water_leak_detector, presence, environment_monitor).
    """
    CALLS.append(("get_table_schema", {"table": table}))
    row = _TABLES.get(table)
    if not row:
        return json.dumps({"table": table, "columns": [], "note": "unknown table"})
    return json.dumps({"table": table, "columns": [{"name": c, "sample_value": v} for c, v in row.items()]}, indent=2)


@tool
def query_iot_data(table: str, limit: int = 20, where: str = "") -> str:
    """Query rows from a specific IoT device table.

    Call get_table_schema first to see available columns for filtering.

    Args:
        table: The table name to query (use list_iot_tables to see available tables).
        limit: Maximum number of rows to return (default 20).
        where: Optional SQL WHERE clause (e.g. "water_detected = 'true'"). All
               values are strings, so use single quotes around them.
    """
    CALLS.append(("query_iot_data", {"table": table, "limit": limit, "where": where}))
    row = _TABLES.get(table)
    if not row:
        return json.dumps({"data": [], "row_count": 0, "note": "unknown table"})
    rows = [row, {**row, "device_id": row["device_id"] + "b"}, {**row, "device_id": row["device_id"] + "c"}]
    return json.dumps({"data": rows[:limit], "row_count": len(rows[:limit])}, indent=2)


# System prompt mirrors reachy_assistant.py (incl. the injected move-name list).
SYSTEM_PROMPT = (
    "You are Reachy, a small friendly desk robot with a camera. You were just "
    "woken by name and given a spoken request. Choose a tool only if it is needed "
    "to answer.\n"
    "- To SEE or answer anything about the physical scene/room/person in front of "
    "you, call look_and_describe and pass the user's visual question as 'question' "
    "(e.g. 'what color is the mug?'); leave it empty for a general description.\n"
    "- To EXPRESS a feeling, or to react to the sentiment/intent of a message, call "
    "play_emotion with exactly ONE move name chosen from the list provided below. "
    "Match the move to the mood (e.g. praise -> success1 or proud1, bad news -> sad1, "
    "a greeting -> welcoming1, a joke -> laughing1). Never invent a name.\n"
    "- To answer questions about IoT sensor data, query the data lake. Discover "
    "first, never guess: call list_iot_tables to see what tables exist, then "
    "get_table_schema to see a table's columns, then query_iot_data with the right "
    "table, limit, and an optional SQL WHERE clause. All column values are strings, "
    "so quote them (e.g. motion_detected = 'true').\n"
    "If the request needs no tool, just answer. After using tools, ALWAYS reply "
    "with ONE short, natural spoken sentence stating the answer — never read raw "
    "JSON, table dumps, or column lists aloud. No markdown, no special characters."
    "\n\nValid play_emotion move names: " + ", ".join(EMOTION_NAMES) + "."
)


def build_agent() -> Agent:
    return Agent(
        model=OllamaModel(host=OLLAMA_HOST, model_id=MODEL_ID),
        tools=[look_and_describe, play_emotion, list_emotion_moves,
               list_iot_tables, get_table_schema, query_iot_data],
        system_prompt=SYSTEM_PROMPT,
    )


def run_case(agent: Agent, title: str, prompt: str, check) -> bool:
    CALLS.clear()
    print(f"\n\033[1m>>> [{title}] {prompt}\033[0m")
    text = str(agent(prompt)).strip()
    seq = [c[0] for c in CALLS]
    print(f"    answer       : {text[:200]}")
    print(f"    call sequence: {seq or '(none)'}")
    ok, why = check(text, CALLS)
    print(f"    \033[1;{'32' if ok else '31'}m{'PASS' if ok else 'FAIL'}\033[0m — {why}")
    return ok


# ─── Checks (mirror how the real assistant expects each tool to be used) ───── #
def check_vision(text, calls):
    names = [c[0] for c in calls]
    if "look_and_describe" not in names:
        return False, "look_and_describe was not called"
    return True, "vision tool called"


def check_emotion(text, calls):
    pe = [c for c in calls if c[0] == "play_emotion"]
    if not pe:
        return False, "play_emotion was not called"
    name = pe[-1][1].get("name", "")
    if name not in EMOTION_NAMES:
        return False, f"invalid move name {name!r}"
    return True, f"played a valid move: {name}"


def check_datalake(text, calls):
    names = [c[0] for c in calls]
    if "list_iot_tables" not in names:
        return False, "did not discover tables first (list_iot_tables missing)"
    q = [c for c in calls if c[0] == "query_iot_data"]
    if not q:
        return False, "never queried (query_iot_data missing)"
    if not any("water" in str(c[1].get("table", "")).lower() for c in q):
        return False, "queried the wrong table (expected water_leak_detector)"
    return True, "discovered then queried the water_leak_detector table"


def main() -> int:
    print(f"Model: {MODEL_ID} @ {OLLAMA_HOST}  (mirroring the real Reachy tool surface)")
    try:
        agent = build_agent()
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] could not build agent: {e}")
        return 2

    results = [
        run_case(agent, "vision",
                 "How many monitors can you see in front of you?", check_vision),
        run_case(agent, "emotion",
                 "React to this message by playing an emotion: great job team, we hit the target!", check_emotion),
        run_case(agent, "datalake",
                 "How many water leaks has the water leak detector recorded? Discover the tables first.",
                 check_datalake),
    ]

    n, total = sum(results), len(results)
    print(f"\n\033[1;33m===== {n}/{total} Reachy-tool cases passed =====\033[0m")
    print("NEMOTRON ON THE REACHY TOOL SURFACE: " +
          ("\033[1;32mREADY ✅\033[0m" if n == total else "\033[1;31mNOT READY ❌\033[0m"))
    return 0 if n == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
