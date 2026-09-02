"""Reachy media bus — one owner per device, fan out to many independent processes.

`/dev/video0` (V4L2) and the ALSA mic are single-opener devices. This module makes
ONE broker process own each one and republish the live stream over a Unix domain
socket, so any number of independent processes (the assistant's own face-tracker,
idle-watcher and clip-recorder, plus brand-new tools you add later) can each consume
the same camera frames and mic audio at once.

Roles (run as standalone owner processes):
    python media_bus.py camera     # owns /dev/video0, publishes JPEG frames
    python media_bus.py audio      # owns the mic,      publishes S16LE/16k/mono PCM

Client API (used by reachy_assistant.py and any new subscriber):
    broker_available("camera")             -> is the camera broker socket present?
    for seq, ts, jpeg in subscribe(sock):  -> raw payloads
    for frame in camera_frames():          -> decoded BGR numpy frames
    mic = MicReader()                      -> drop-in for an arecord Popen
                                              (mic.stdout.read(n), .terminate(), ...)

Wire format:  [4-byte len][payload];  payload = [seq u64][ts f64][body].
Backpressure is per-subscriber (bounded queue + sender thread): a slow consumer
drops its own oldest frames and never stalls the device loop or its peers.

Proven by poc_fanout/ (run_proof.py): one camera + one mic served the real Cosmos
vision workload plus several other processes concurrently, 0 drops on survivors.
"""

from __future__ import annotations

import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# socket paths / config
# --------------------------------------------------------------------------- #
CAM_SOCK = os.environ.get("REACHY_CAM_SOCK", "/tmp/reachy_cam.sock")
AUD_SOCK = os.environ.get("REACHY_AUD_SOCK", "/tmp/reachy_audio.sock")

# audio format (fixed so subscribers need no negotiation)
AUDIO_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_BYTES = 2                       # S16_LE
AUDIO_CHUNK_MS = int(os.environ.get("REACHY_AUD_MS", "100"))

_SOCKS = {"camera": CAM_SOCK, "audio": AUD_SOCK}

_LEN = struct.Struct(">I")
_HDR = struct.Struct(">Qd")


# --------------------------------------------------------------------------- #
# length-prefixed send / recv + payload header
# --------------------------------------------------------------------------- #
def send_msg(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(_LEN.pack(len(payload)) + payload)


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def recv_msg(sock: socket.socket) -> bytes | None:
    head = _recv_exactly(sock, _LEN.size)
    if head is None:
        return None
    (length,) = _LEN.unpack(head)
    return _recv_exactly(sock, length)


def _pack(seq: int, body: bytes) -> bytes:
    return _HDR.pack(seq, time.time()) + body


def _unpack(payload: bytes) -> tuple[int, float, bytes]:
    seq, ts = _HDR.unpack_from(payload, 0)
    return seq, ts, payload[_HDR.size:]


# --------------------------------------------------------------------------- #
# Broker
# --------------------------------------------------------------------------- #
class _Client:
    def __init__(self, conn: socket.socket, qdepth: int):
        self.conn = conn
        self.q: queue.Queue = queue.Queue(maxsize=qdepth)
        self.dropped = 0
        self.alive = True


class Broker:
    def __init__(self, path: str, qdepth: int = 3):
        self.path = path
        self.qdepth = qdepth
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._seq = 0
        if os.path.exists(path):
            os.unlink(path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(16)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            cl = _Client(conn, self.qdepth)
            with self._lock:
                self._clients.append(cl)
            threading.Thread(target=self._send_loop, args=(cl,), daemon=True).start()

    def _send_loop(self, cl: _Client) -> None:
        while cl.alive:
            payload = cl.q.get()
            if payload is None:
                break
            try:
                send_msg(cl.conn, payload)
            except OSError:
                cl.alive = False
                break
        try:
            cl.conn.close()
        except OSError:
            pass

    @property
    def n_subscribers(self) -> int:
        with self._lock:
            return sum(1 for c in self._clients if c.alive)

    def publish(self, body: bytes) -> None:
        self._seq += 1
        payload = _pack(self._seq, body)
        with self._lock:
            clients = list(self._clients)
        for cl in clients:
            if not cl.alive:
                continue
            try:
                cl.q.put_nowait(payload)
            except queue.Full:
                try:
                    cl.q.get_nowait()
                    cl.q.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    pass
                cl.dropped += 1

    def close(self) -> None:
        try:
            self._srv.close()
        finally:
            with self._lock:
                for c in self._clients:
                    c.alive = False
                    try:
                        c.q.put_nowait(None)
                    except queue.Full:
                        pass
            if os.path.exists(self.path):
                try:
                    os.unlink(self.path)
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
# Subscriber primitives
# --------------------------------------------------------------------------- #
def broker_socket(role: str) -> str:
    return _SOCKS[role]


def broker_available(role: str) -> bool:
    """True if the broker for this role is up (its socket exists and accepts)."""
    path = _SOCKS[role]
    if not os.path.exists(path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def subscribe(path: str, retries: int = 50, delay: float = 0.1):
    """Generator yielding (seq, ts, body) from a broker. Blocks until connected."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(retries):
        try:
            sock.connect(path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(delay)
    else:
        raise RuntimeError(f"could not connect to broker at {path}")
    try:
        while True:
            payload = recv_msg(sock)
            if payload is None:
                return
            yield _unpack(payload)
    finally:
        sock.close()


def camera_frames(path: str | None = None):
    """Yield decoded BGR numpy frames from the camera broker."""
    import cv2
    import numpy as np
    for seq, ts, jpeg in subscribe(path or CAM_SOCK):
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None:
            yield frame


class _MicStdout:
    """File-like .read(n) over the audio broker — drop-in for arecord's stdout."""

    def __init__(self, gen):
        self._gen = gen
        self._buf = bytearray()
        self._eof = False

    def read(self, n: int) -> bytes:
        while len(self._buf) < n and not self._eof:
            try:
                _seq, _ts, body = next(self._gen)
                self._buf += body
            except StopIteration:
                self._eof = True
                break
        if not self._buf:
            return b""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class MicReader:
    """Drop-in replacement for the arecord subprocess.Popen used by the voice loop.

    Exposes .stdout.read(n), .terminate(), .wait(), .kill(). Each MicReader is a
    fresh subscription that starts from live audio, so the existing
    "terminate + _start_mic()" pattern still drops task-time backlog (the closed
    socket's buffered audio is discarded; the new one begins at 'now').
    """

    def __init__(self, path: str | None = None):
        self._gen = subscribe(path or AUD_SOCK)
        self.stdout = _MicStdout(self._gen)

    def terminate(self) -> None:
        try:
            self._gen.close()
        except Exception:  # noqa: BLE001
            pass

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminate()


# --------------------------------------------------------------------------- #
# Device owners (the broker processes)
# --------------------------------------------------------------------------- #
def _log(role: str, msg: str) -> None:
    print(f"[media-bus:{role}] {msg}", flush=True)


def run_camera_broker() -> int:
    """Own the camera once and publish JPEG frames. MJPG is essential: OpenCV's
    default YUYV is hard-capped near 5 fps on this camera; MJPG gives ~30+."""
    import signal

    import cv2

    camera = int(os.environ.get("REACHY_CAMERA", "0"))
    cap_w = int(os.environ.get("REACHY_CAM_CAP_W", "1920"))
    cap_h = int(os.environ.get("REACHY_CAM_CAP_H", "1080"))
    pub_w = int(os.environ.get("REACHY_CAM_W", "640"))
    pub_h = int(os.environ.get("REACHY_CAM_H", "480"))
    fps = float(os.environ.get("REACHY_CAM_FPS", "30"))
    jpeg_q = int(os.environ.get("REACHY_CAM_Q", "70"))

    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))

    cap = cv2.VideoCapture(camera, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cap_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cap_h)
    if not cap.isOpened():
        _log("camera", f"FATAL: cannot open camera {camera} (held by another process?)")
        return 1

    broker = Broker(CAM_SOCK)
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode(errors="ignore")
    _log("camera", f"owning /dev/video{camera} capture {cap_w}x{cap_h} fourcc={fourcc}, "
                   f"publishing {pub_w}x{pub_h} JPEG @~{fps:.0f}fps on {CAM_SOCK}")

    period = 1.0 / fps
    enc = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_q]
    published = 0
    t_stat = time.time()
    since = 0
    try:
        while not stop["v"]:
            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            if frame.shape[1] != pub_w or frame.shape[0] != pub_h:
                frame = cv2.resize(frame, (pub_w, pub_h))
            ok, buf = cv2.imencode(".jpg", frame, enc)
            if not ok:
                continue
            broker.publish(buf.tobytes())
            published += 1
            since += 1
            now = time.time()
            if now - t_stat >= 10.0:
                _log("camera", f"{since/(now-t_stat):.1f} fps  subscribers={broker.n_subscribers}  "
                               f"total={published}")
                t_stat = now
                since = 0
            dt = period - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)
    finally:
        cap.release()
        broker.close()
        _log("camera", f"stopped. published {published} frames.")
    return 0


def _detect_audio_card() -> str:
    try:
        out = subprocess.check_output(["arecord", "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return os.environ.get("REACHY_AUDIO_CARD", "0")
    for line in out.splitlines():
        low = line.lower()
        if "reachy mini audio" in low or "respeaker" in low:
            try:
                return line.split("card", 1)[1].split(":", 1)[0].strip()
            except IndexError:
                pass
    return os.environ.get("REACHY_AUDIO_CARD", "0")


def run_audio_broker() -> int:
    """Own the mic once (via arecord) and publish fixed-size PCM chunks."""
    import signal

    card = os.environ.get("REACHY_AUDIO_CARD") or _detect_audio_card()
    dev = os.environ.get("MIC_DEV", f"plughw:{card}")
    chunk_frames = int(AUDIO_RATE * AUDIO_CHUNK_MS / 1000)
    chunk_bytes = chunk_frames * AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES

    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))

    proc = subprocess.Popen(
        ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(AUDIO_RATE),
         "-c", str(AUDIO_CHANNELS), "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    broker = Broker(AUD_SOCK)
    _log("audio", f"owning mic {dev}, publishing {AUDIO_CHUNK_MS}ms PCM chunks "
                  f"(S16LE/{AUDIO_RATE}/mono) on {AUD_SOCK}")

    published = 0
    t_stat = time.time()
    try:
        while not stop["v"]:
            data = proc.stdout.read(chunk_bytes)
            if not data or len(data) < chunk_bytes:
                if proc.poll() is not None:
                    err = proc.stderr.read().decode(errors="ignore")[:200]
                    _log("audio", f"FATAL: arecord ended (mic busy?). {err}")
                    return 1
                continue
            broker.publish(data)
            published += 1
            now = time.time()
            if now - t_stat >= 10.0:
                _log("audio", f"{published} chunks  subscribers={broker.n_subscribers}")
                t_stat = now
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            proc.kill()
        broker.close()
        _log("audio", f"stopped. published {published} chunks.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("camera", "audio"):
        print("usage: python media_bus.py {camera|audio}", file=sys.stderr)
        return 2
    return run_camera_broker() if argv[1] == "camera" else run_audio_broker()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
