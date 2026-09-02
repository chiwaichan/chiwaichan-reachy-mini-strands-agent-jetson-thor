# Local models — the on-device AI stack

Everything that makes Reachy think, see, hear, and speak runs **locally on the
NVIDIA Jetson Thor**. There is no cloud dependency in the default configuration and
**$0 per interaction** — the only opt-in cloud path is swapping the agent's brain to
Amazon Bedrock. This page documents each local model in detail: what it is, where it
runs, how it's loaded and invoked, and the design choices behind it.

![Local model stack](local_models.png)

## At a glance

| Model | Role | Where | Size | Cloud? |
|-------|------|-------|------|--------|
| **Nemotron** (`nemotron-3-nano:30b`) | Agent brain — reasoning + tool-calling | Ollama runtime, GPU | large (~GBs) | No ($0) |
| **Cosmos Reason 2** (`nvidia/Cosmos-Reason2-2B`) | Vision-language model — "the eyes" | `.venv-cosmos`, GPU | ~5GB | No ($0) |
| **Vosk** (`vosk-model-small-en-us-0.15`) | Wake word + speech-to-text | `.venv`, CPU | ~40MB | No ($0) |
| **Piper** (`en_US-lessac-medium`) | Neural text-to-speech | `.venv`, CPU | ~60MB | No ($0) |
| *Bedrock Nova 2 Lite* | *Optional* agent brain swap | *AWS cloud* | — | Yes (opt-in) |

Two Python virtual environments keep the dependency stacks apart:

- **`.venv`** — the assistant: `reachy-mini`, `vosk`, `strands-agents`, `boto3`,
  `awsiotsdk`, `opencv-python-headless`, `ollama`, `piper-tts`.
- **`.venv-cosmos`** — the heavy CUDA stack for the VLM only (see [Cosmos](#2-cosmos-reason-2--vision-language-model-the-eyes)).

---

## 1. Nemotron — the agent brain (reasoning + tool-calling)

The Strands agent's reasoning model. On every wake a **fresh agent** is built around
it, it decides which tool to call (vision, emotion, or data-lake), and is then torn
down. This is the model that replaced Amazon Nova 2 Lite as the **default** on this
branch — same agent, same tools, same system prompt, now $0 and offline.

| Property | Value |
|----------|-------|
| Model id | `nemotron-3-nano:30b` (env `NEMOTRON_MODEL`) |
| Served by | **Ollama** at `http://localhost:11434` (env `OLLAMA_HOST`) |
| Strands binding | `strands.models.ollama.OllamaModel(host=OLLAMA_HOST, model_id=NEMOTRON_MODEL)` |
| Requirement | Must advertise the **`tools`** capability (function calling) |
| Reasoning style | Emits `<think>…</think>` blocks that are stripped before speaking |
| Cost | **$0** — runs locally on the Thor GPU |

### How it's wired

`reachy_assistant.py` builds the model based on the `LLM_BACKEND` switch, so the rest
of the agent is identical whether the brain is local or cloud:

```python
def _build_model():
    if LLM_BACKEND == "bedrock":
        return BedrockModel(model_id=MODEL_ID, region_name=AWS_REGION)
    from strands.models.ollama import OllamaModel
    return OllamaModel(host=OLLAMA_HOST, model_id=NEMOTRON_MODEL)
```

The agent is given six tools and a system prompt that enforces *discover-before-guess*
on the data lake and a **single short spoken sentence** as the final reply (never raw
JSON). Two agents use Nemotron:

- **Per-wake task agent** (`handle_wake`) — the full tool surface: `look_and_describe`,
  `play_emotion`, `list_emotion_moves`, `list_iot_tables`, `get_table_schema`,
  `query_iot_data`.
- **Idle presence agent** (`_run_presence_agent`) — a minimal agent with only
  `report_presence`, fed the idle watcher's camera observation.

### Reasoning-model hygiene

Nemotron is a reasoning model and surfaces its chain of thought inside
`<think>…</think>`. Before anything is spoken, that is stripped:

```python
def _clean_reply(text: str) -> str:
    return re.sub(r"(?is)<think>.*?</think>", "", text or "").strip()
```

### Cost guard

Because a single data-lake question can take several model calls
(discover → schema → query → answer), a hard cap bounds latency and (on Bedrock) cost.
`ModelCallBudget` is a Strands hook on `BeforeModelCallEvent`:

```python
def _before(self, _e: BeforeModelCallEvent) -> None:
    self.count += 1
    if self.count > self.max_calls:
        raise RuntimeError(f"Model-call budget exceeded ({self.max_calls}).")
```

Default `MAX_MODEL_CALLS=12`. Local Nemotron runs report `$0`; Bedrock runs compute a
rough cost from `PRICE_IN_PER_M` / `PRICE_OUT_PER_M`.

### Setup & validation

`nemotron_setup.sh` is the local-LLM bring-up. It is idempotent and runs four checks:

1. **Ollama installed** — installs via `ollama.com/install.sh` if missing.
2. **Ollama serving** — `systemctl start ollama` or `nohup ollama serve`; waits on `/api/version`.
3. **Model pulled** — `ollama pull nemotron-3-nano:30b` if absent; then asserts the
   model `ollama show` output advertises **`tools`** (required for the agent).
4. **Generation smoke test** — a one-line prompt to warm the model into the GPU.

`test_nemotron_agent.py` then proves Strands tool-calling works on Nemotron against a
1:1 mirror of the real assistant's tool surface (same names, params, system prompt).

### Switching to Bedrock

```bash
LLM_BACKEND=bedrock ./reachy_assistant.sh   # Amazon Nova 2 Lite, us-east-1
```

| | Nemotron (default) | Bedrock Nova 2 Lite |
|--|--------------------|---------------------|
| Location | On-device (Thor GPU) | AWS cloud |
| Cost | $0 | ~$0.06/1M in, ~$0.24/1M out |
| Offline | Yes | No (needs network + AWS creds) |
| Model id | `nemotron-3-nano:30b` | `us.amazon.nova-2-lite-v1:0` |
| Tools / prompt | Identical | Identical |

---

## 2. Cosmos Reason 2 — vision-language model ("the eyes")

The robot's vision. When the agent calls `look_and_describe`, a frame or short clip
from the camera is sent to **NVIDIA Cosmos Reason 2** (a Qwen3-VL model) running
locally, which answers a question about the scene. Used for visual Q&A, the idle
presence watcher, and the standalone `cosmos_describe.py` CLI.

| Property | Value |
|----------|-------|
| Model id | `nvidia/Cosmos-Reason2-2B` (env `COSMOS_MODEL`) |
| Architecture | Qwen3-VL (`Qwen3VLForConditionalGeneration`) |
| Precision | `float16` |
| Device map | `{"": "cuda:0"}` — all weights forced onto the GPU |
| Attention | `attn_implementation="sdpa"` |
| Processor | `AutoProcessor`, optional `qwen_vl_utils.process_vision_info` |
| Decode | Greedy (`do_sample=False`), `max_new_tokens=512` default |
| Download | ~5GB on first run |
| Cost | **$0** — local on the Thor GPU |

### Loading — the Jetson unified-memory gotcha

On Jetson's unified memory, `device_map="auto"` wrongly offloads weights to the CPU
(params land on the meta device), which is slow. The loader pins everything to the GPU
explicitly:

```python
model = Qwen3VLForConditionalGeneration.from_pretrained(
    model_id, dtype=torch.float16, device_map={"": "cuda:0"}, attn_implementation="sdpa"
)
```

It raises immediately if CUDA is unavailable — Cosmos requires the GPU.

### Inference: video clip vs. single frame

`run_inference` accepts either a multi-frame clip (`media_type="video"`, fed at
`fps=4`) or a single image (`media_type="image"`, just `frames[0]` — much faster, used
for cheap idle presence checks). Cosmos emits `<think>reasoning</think>` then the
answer; the helper splits on `</think>` and returns `(answer, thinking)`.

### Warm server vs. one-shot CLI — and why

Cold-loading ~5GB on every glance would make vision unusable, so `cosmos_server.py`
loads the model **once** and keeps it resident behind a tiny localhost HTTP API:

| Endpoint | Behavior |
|----------|----------|
| `GET /health` | `200 {"ready": true}` once loaded (`503` before) |
| `POST /look` | `{"answer", "seconds", "mode"}` — body: `question`, `seconds`, `fps`, `max_new_tokens`, `image`, `image_b64` |

- A single `Lock` serializes inference — the GPU and camera are single-tenant.
- `look_and_describe` prefers the warm server and **falls back** to a one-shot
  `cosmos_describe.py` subprocess if it's down — so vision degrades gracefully rather
  than failing.
- When the in-process **camera owner** (face tracking) is up, the assistant passes the
  latest frame as base64 (`image_b64`) so the server doesn't reopen the camera — this
  is what lets head-tracking and vision share one camera without V4L2 contention.

The CLI and the server share **one code path** (`load_model`, `capture_clip`,
`capture_frame`, `frame_question`, `run_inference` in `cosmos_describe.py`), so behavior
is identical either way.

### The `.venv-cosmos` CUDA stack

Built by `cosmos_describe.sh` against the **Jetson cu130** wheel index, installed only
if `torch.cuda.is_available()` isn't already satisfied (keeps repeat runs instant):

| Package | Pin / note |
|---------|-----------|
| `torch` | `2.11.0` from `https://pypi.jetson-ai-lab.io/sbsa/cu130/` |
| `torchvision` | `0.25.0` `--no-deps` (it over-pins torch 2.10 otherwise) |
| `transformers`, `accelerate`, `qwen-vl-utils`, `opencv-python`, `pillow`, `numpy` | latest |
| Python | 3.12 (cu130 wheels are cp312) |

### Try it directly

```bash
./cosmos_describe.sh --seconds 5 --question "What is the person doing?" --show-thinking
./test_cosmos_look.sh    # exercises the warm server's /look endpoint
```

---

## 3. Vosk — wake word & speech-to-text

The fully offline ears. Vosk does both jobs with zero LLM cost: it listens for the
wake word while idle, and after a wake it transcribes the spoken request.

| Property | Value |
|----------|-------|
| Model | `vosk-model-small-en-us-0.15` (env `VOSK_MODEL`, ~40MB) |
| Recognizer | `KaldiRecognizer(model, 16000)` — 16 kHz mono |
| Audio source | `arecord … -f S16_LE -r 16000 -c 1 -t raw` (raw PCM stream) |
| Cost | **$0** — fully offline |

### Wake-word matching

"reachy" is **not** in Vosk's English lexicon, so a constrained grammar would drop it.
Instead the assistant matches against the homophone renderings Vosk actually emits:

```python
WAKE_TOKENS = ("reachy", "reach", "richie", "ritchie", "reachie")
```

On the idle stream, partial and final results are scanned for any of these tokens. On a
match (and only if not already busy), the head raises as an "I'm listening" cue and a
**fresh** `KaldiRecognizer` transcribes the request — so the wake word itself isn't
carried into the request. Capture ends on a natural pause (Vosk end-of-utterance) or
after `LISTEN_SECONDS` (default 8) as a backstop.

`voice_wake.py` is a standalone demo of just the wake-word stage (no agent, no LLM).

---

## 4. Piper — neural text-to-speech

The voice. The agent's final sentence is synthesized locally and played through the
Reachy speaker.

| Property | Value |
|----------|-------|
| Voice | `en_US-lessac-medium` (`.onnx` + `.json`, ~60MB) |
| Loader | `piper.PiperVoice.load(PIPER_VOICE)` → `synthesize_wav` |
| Playback | `aplay -D plughw:<card>` to the Reachy ALSA card |
| Cost | **$0** — offline |

### Graceful fallback chain

TTS degrades cleanly so the robot is never mute by surprise:

1. **Piper** if the voice file is present and loads.
2. **`espeak-ng`** if installed (lower quality, always intelligible).
3. **Print only** if neither is available (logs the line instead of speaking it).

The Piper voice is downloaded on first run by `reachy_assistant.sh` only if the `piper`
module imported successfully.

---

## Design principles

- **Local-first, $0 idle.** While resting, only the Vosk listener runs — no LLM, no
  GPU inference, no cost. Models do work only when there's something to do.
- **Ephemeral agents.** The Nemotron-backed agent exists only between wake and reply,
  then is `del`'d and garbage-collected — no long-lived LLM process.
- **Single GPU/camera tenant.** A lock serializes Cosmos inference and a single camera
  owner shares frames, so vision, face-tracking, and idle checks coexist on one GPU and
  one camera without contention.
- **Graceful degradation everywhere.** Warm server → subprocess (vision); Piper →
  espeak-ng → print (speech); local → Bedrock (brain, opt-in). A missing component
  downgrades the experience instead of breaking it.
- **Shared code paths.** The CLI and the warm server share one Cosmos code path; the
  local and cloud brains share one agent/tool/prompt surface — so behavior is consistent
  and the validation scripts test the real thing.

## Re-render this page's diagram

```bash
python3 diagrams/local_models.py   # -> docs/local_models.png
```
