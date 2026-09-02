"""Warm Cosmos Reason 2 server — load the VLM once, answer many look requests.

Runs in .venv-cosmos (same env as cosmos_describe.py). It loads Cosmos Reason 2
at startup and keeps it resident, then serves a tiny localhost HTTP API so the
assistant's look_and_describe tool gets near-interactive vision instead of paying
a ~5GB cold start on every glance. Fully local, Cosmos-only, $0 per look.

  GET  /health        -> 200 {"ready": true} once the model is loaded (503 before)
  POST /look          -> {"answer": str, "seconds": float}
       body (JSON): {"question": "...", "seconds": 4, "fps": 4, "max_new_tokens": 512}

Uses only the standard library for serving (no extra deps in the Cosmos venv).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

# Shared code path with the CLI (model load, inference, question framing).
from cosmos_describe import (
    capture_clip, capture_frame, frame_question, load_model, run_inference,
)

MODEL_ID = os.environ.get("COSMOS_MODEL", "nvidia/Cosmos-Reason2-2B")
PORT = int(os.environ.get("COSMOS_PORT", "8077"))
# Bind address. Defaults to loopback (single-box mode, unchanged). Set
# COSMOS_BIND=0.0.0.0 to expose the vision API to LAN clients (split deployment).
BIND = os.environ.get("COSMOS_BIND", "127.0.0.1")
CAMERA = int(os.environ.get("REACHY_CAMERA", "0"))

_model = None
_processor = None
_lock = Lock()  # GPU + camera are single-tenant; serialize look requests


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silence default per-request logging
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200 if _model is not None else 503, {"ready": _model is not None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/look":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": f"bad request: {e}"})
            return

        seconds = float(req.get("seconds", 4))
        fps = float(req.get("fps", 4))
        max_new = int(req.get("max_new_tokens", 512))
        image_mode = bool(req.get("image", False))  # single frame instead of a clip
        image_b64 = req.get("image_b64")             # caller-supplied frame (shared camera)
        prompt = frame_question(req.get("question", ""))

        t0 = time.time()
        try:
            with _lock:  # one inference at a time (GPU)
                with tempfile.TemporaryDirectory(prefix="cosmos_clip_") as tmp:
                    if image_b64:
                        # The caller owns the camera and sent us a frame — don't
                        # touch the camera ourselves (avoids V4L2 contention).
                        import base64
                        p = os.path.join(tmp, "frame.jpg")
                        with open(p, "wb") as f:
                            f.write(base64.b64decode(image_b64))
                        frames, mode = [f"file://{p}"], "uploaded"
                    elif image_mode:
                        frames, mode = [capture_frame(CAMERA, tmp, quiet=True)], "image"
                    else:
                        frames, mode = capture_clip(CAMERA, seconds, fps, tmp, quiet=True), "video"
                    media_type = "video" if mode == "video" else "image"
                    answer, _ = run_inference(_model, _processor, frames, prompt,
                                              fps=fps, max_new_tokens=max_new, media_type=media_type)
            self._send(200, {"answer": answer, "seconds": round(time.time() - t0, 1), "mode": mode})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})


def main() -> int:
    global _model, _processor
    print(f"[cosmos-server] loading {MODEL_ID} (first run downloads ~5GB)...", flush=True)
    t0 = time.time()
    _model, _processor = load_model(MODEL_ID)
    print(f"[cosmos-server] model ready in {time.time() - t0:.0f}s; "
          f"serving on http://{BIND}:{PORT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
