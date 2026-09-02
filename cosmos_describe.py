"""Local scene description with NVIDIA Cosmos Reason 2 — NO cloud, NO Bedrock.

Captures a few seconds of video from the Reachy Mini camera (direct V4L2, no
daemon needed) and runs Cosmos Reason 2 (Qwen3-VL) locally on the Jetson Thor
GPU to describe the scene. Zero AWS / Bedrock tokens consumed.

The model-load / inference / question-framing helpers are importable so the warm
server (cosmos_server.py) and this CLI share exactly one code path.

Usage:
    python cosmos_describe.py --seconds 5 --fps 4 --camera 0
    python cosmos_describe.py --question "What is the person doing?" --show-thinking
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time

import cv2

DEFAULT_PROMPT = (
    "Describe this scene in detail: the setting, objects, any people and what "
    "they are doing. Be specific and concise."
)


def frame_question(question: str) -> str:
    """Turn a bare question into a concise, scene-grounded prompt.

    Shared by the CLI (--question) and the warm server so a spoken question like
    "what color is the mug?" is answered specifically instead of triggering a
    generic scene description. Empty question -> the default describe prompt.
    """
    q = (question or "").strip()
    if not q:
        return DEFAULT_PROMPT
    return (
        "Looking through your camera at the scene in front of you, answer this "
        f"question concisely, based only on what you can see: {q}"
    )


def capture_clip(camera: int, seconds: float, fps: float, outdir: str, quiet: bool = False) -> list[str]:
    """Grab ~seconds*fps frames from the camera, save as JPGs, return file:// paths."""
    cap = cv2.VideoCapture(camera, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(
            f"Could not open camera index {camera} (/dev/video{camera}). "
            "Is it connected, and is the Reachy daemon (which may hold the camera) stopped?"
        )
    paths: list[str] = []
    interval = 1.0 / fps
    t_end = time.time() + seconds
    next_t = time.time()
    i = 0
    if not quiet:
        print(f"Recording {seconds:.0f}s @ {fps:.0f} fps from camera {camera}...")
    while time.time() < t_end:
        ok, frame = cap.read()
        if not ok:
            continue
        now = time.time()
        if now >= next_t:
            p = os.path.join(outdir, f"frame_{i:04d}.jpg")
            cv2.imwrite(p, frame)
            paths.append(f"file://{p}")
            i += 1
            next_t += interval
    cap.release()
    if not paths:
        raise RuntimeError("No frames captured — camera opened but returned no images.")
    if not quiet:
        print(f"Captured {len(paths)} frames.")
    return paths


def capture_frame(camera: int, outdir: str, quiet: bool = False) -> str:
    """Grab a SINGLE frame from the camera; return its file:// path.

    Much faster than capture_clip — no multi-second recording window — so it's
    ideal for cheap, frequent checks (e.g. idle presence polling).
    """
    cap = cv2.VideoCapture(camera, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(
            f"Could not open camera index {camera} (/dev/video{camera}). "
            "Is it connected, and is the Reachy daemon (which may hold the camera) stopped?"
        )
    frame = None
    for _ in range(8):  # skip the first few warm-up reads V4L2 often returns empty
        ok, f = cap.read()
        if ok:
            frame = f
    cap.release()
    if frame is None:
        raise RuntimeError("No frame captured — camera opened but returned no image.")
    p = os.path.join(outdir, "frame.jpg")
    cv2.imwrite(p, frame)
    if not quiet:
        print(f"Captured 1 frame from camera {camera}.")
    return f"file://{p}"


def load_model(model_id: str):
    """Load Cosmos Reason 2 (Qwen3-VL) fully onto the GPU. Raises if CUDA is missing."""
    import torch
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available — Cosmos Reason 2 needs the GPU.")
    # Force all weights onto the GPU. On Jetson's unified memory device_map="auto"
    # wrongly offloads to "cpu" (params land on the meta device), which is slow.
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        model_id, dtype=torch.float16, device_map={"": "cuda:0"}, attn_implementation="sdpa"
    )
    processor = AutoProcessor.from_pretrained(model_id)
    return model, processor


def run_inference(model, processor, frames: list[str], prompt: str,
                  fps: float = 4.0, max_new_tokens: int = 512,
                  media_type: str = "video") -> tuple[str, str]:
    """Run Cosmos on the captured frames; return (answer, thinking).

    media_type "video" feeds the whole clip; "image" feeds just frames[0] (faster,
    for single-frame checks).
    """
    import torch

    if media_type == "image":
        media = {"type": "image", "image": frames[0]}
    else:
        media = {"type": "video", "video": frames, "fps": fps}
    messages = [{"role": "user", "content": [media, {"type": "text", "text": prompt}]}]

    # Prefer qwen_vl_utils if present; otherwise let the processor handle it.
    try:
        from qwen_vl_utils import process_vision_info
        image_inputs, video_inputs = process_vision_info(messages)
    except Exception:  # noqa: BLE001
        image_inputs, video_inputs = None, None

    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = processor(
        text=[text], images=image_inputs, videos=video_inputs,
        return_tensors="pt", padding=True,
    ).to(model.device)

    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    gen = out[:, inputs["input_ids"].shape[1]:]
    result = processor.batch_decode(gen, skip_special_tokens=True)[0].strip()

    # Cosmos Reason 2 emits <think>reasoning</think> then the answer.
    thinking, answer = "", result
    if "</think>" in result:
        thinking, answer = result.split("</think>", 1)
        thinking = thinking.replace("<think>", "").strip()
        answer = answer.strip()
    return (answer or result), thinking


def main() -> int:
    ap = argparse.ArgumentParser(description="Local Cosmos Reason 2 scene description.")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--camera", type=int, default=int(os.environ.get("REACHY_CAMERA", "0")))
    ap.add_argument("--prompt", type=str, default=DEFAULT_PROMPT, help="Literal prompt to send.")
    ap.add_argument("--question", type=str, default="",
                    help="A question to answer about the scene (gets framed). Overrides --prompt.")
    ap.add_argument("--max-new-tokens", type=int, default=512)
    ap.add_argument("--show-thinking", action="store_true", help="Also print the model's reasoning.")
    ap.add_argument("--quiet", action="store_true", help="Print ONLY the answer (for tool/agent use).")
    ap.add_argument("--model", type=str, default=os.environ.get("COSMOS_MODEL", "nvidia/Cosmos-Reason2-2B"))
    args = ap.parse_args()
    q = args.quiet

    prompt = frame_question(args.question) if args.question.strip() else args.prompt

    import torch
    if not torch.cuda.is_available():
        print("[fatal] CUDA not available — Cosmos Reason 2 needs the GPU. Check the torch install.")
        return 1
    if not q:
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    with tempfile.TemporaryDirectory(prefix="cosmos_clip_") as tmp:
        frames = capture_clip(args.camera, args.seconds, args.fps, tmp, quiet=q)

        if not q:
            print(f"Loading {args.model} (first run downloads ~5GB)...")
        t0 = time.time()
        model, processor = load_model(args.model)
        if not q:
            print(f"Model loaded in {time.time() - t0:.0f}s. Running inference...")

        t1 = time.time()
        answer, thinking = run_inference(
            model, processor, frames, prompt, fps=args.fps, max_new_tokens=args.max_new_tokens
        )

    if q:
        print(answer)  # clean, single-block output for the agent tool to consume
        return 0
    print(f"\n(inference {time.time() - t1:.1f}s)")
    if args.show_thinking and thinking:
        print("\n--- reasoning ---\n" + thinking)
    print("\n=== SCENE DESCRIPTION ===\n" + answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
