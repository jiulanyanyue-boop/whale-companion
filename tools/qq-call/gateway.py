"""Loopback-only PCM bridge between Linux QQ audio and the existing AI host."""

from __future__ import annotations

import argparse
import array
import base64
import collections
import contextlib
import hmac
import io
import json
import math
import os
import subprocess
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SpeechBuffer:
    """Collect bounded 20 ms PCM frames through the end of one utterance."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.pre = collections.deque(maxlen=10)
        self.frames = []
        self.silence = 0
        self.voiced = 0

    def feed(self, frame):
        samples = array.array("h", frame)
        rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
        voiced = rms > 330
        if not self.frames:
            self.pre.append(frame)
            if not voiced:
                return None
            self.frames = list(self.pre)
        else:
            self.frames.append(frame)
        self.voiced += int(voiced)
        self.silence = 0 if voiced else self.silence + 1
        if self.silence < 30 and len(self.frames) < 750:
            return None
        pcm = b"".join(self.frames) if self.voiced >= 15 else None
        self.reset()
        return pcm


class AudioGateway:
    """Own the call generation, audio processes, and bounded utterance queue."""

    def __init__(self, token, owner, bridge_url, lease_file, pulse_server):
        self.token, self.owner = token, owner
        self.bridge_url = bridge_url
        self.lease_file = Path(lease_file)
        self.env = {**os.environ, "PULSE_SERVER": pulse_server}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.call = {}
        self.audio_ready = False
        self.error = "qq_bridge_not_ready"
        self.queue = collections.deque(maxlen=2)
        self.pending = {}
        self.played = collections.deque(maxlen=64)
        self.sequence = 0
        self.capture = None
        self.playback = None

    def eligible(self):
        return (
            self.call.get("phase") == "connected"
            and self.call.get("callerUin") == self.owner
            and bool(self.call.get("inviteAt"))
        )

    def snapshot(self):
        with self.lock:
            return {
                "ready": self.audio_ready and not self.error,
                "phase": self.call.get("phase", "unavailable"),
                "call_id": self.call.get("inviteAt", ""),
                "caller_id": self.call.get("callerUin", ""),
                "error": self.error,
                "capabilities": {"incoming": True, "outgoing": False},
            }

    def monitor(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while not self.stop_event.is_set():
            try:
                request = urllib.request.Request(
                    self.bridge_url + "/v1/status",
                    headers={"Authorization": "Bearer " + self.token},
                )
                with opener.open(request, timeout=2) as response:
                    bridge = json.load(response)["data"]
                call = bridge.get("call", {})
                if not isinstance(call, dict):
                    raise TypeError("invalid_call_state")
                error = "" if self.audio_ready else "audio_not_ready"
                native = bridge.get("avHost", {})
                if not bridge.get("listenerRegistered") or not bridge.get(
                    "serviceAvailable"
                ):
                    error = "qq_call_listener_not_ready"
                elif (
                    native.get("loginResultCode") != 0
                    or native.get("loginPosted") is not True
                ):
                    error = "native_login_unconfirmed"
            except (OSError, ValueError, KeyError, TypeError):
                call, error = {}, "qq_bridge_not_ready"
            with self.lock:
                changed = call.get("inviteAt") != self.call.get("inviteAt")
                self.call, self.error = call, error
                if changed or not self.eligible():
                    self.queue.clear()
                    self.pending.clear()
                    if self.playback and self.playback.poll() is None:
                        self.playback.terminate()
                if (
                    self.playback
                    and self.playback.poll() is None
                    and (
                        not self.lease_file.exists()
                        or time.time() - self.lease_file.stat().st_mtime > 15
                    )
                ):
                    self.playback.terminate()
            self.stop_event.wait(0.25)

    def record(self):
        segmenter = SpeechBuffer()
        generation = ""
        while not self.stop_event.is_set():
            try:
                self.capture = subprocess.Popen(
                    [
                        "parec",
                        "--raw",
                        "--device=maibot_qq_speaker.monitor",
                        "--format=s16le",
                        "--rate=16000",
                        "--channels=1",
                        "--latency-msec=20",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=self.env,
                )
                while not self.stop_event.is_set():
                    frame = self.capture.stdout.read(640)
                    if len(frame) != 640:
                        raise RuntimeError("audio_capture_stopped")
                    with self.lock:
                        self.audio_ready = True
                        current = self.call.get("inviteAt", "")
                        if not self.eligible() or current != generation:
                            segmenter.reset()
                            generation = current
                        if not self.eligible():
                            continue
                        pcm = segmenter.feed(frame)
                        if segmenter.voiced == 15:
                            self.pending.clear()
                            self.queue.clear()
                        if (
                            segmenter.voiced >= 15
                            and self.playback
                            and self.playback.poll() is None
                        ):
                            self.playback.terminate()
                        if pcm:
                            output = io.BytesIO()
                            with wave.open(output, "wb") as audio:
                                audio.setnchannels(1)
                                audio.setsampwidth(2)
                                audio.setframerate(16000)
                                audio.writeframes(pcm)
                            self.sequence += 1
                            self.queue.append(
                                {
                                    "call_id": current,
                                    "caller_id": self.owner,
                                    "utterance_id": str(self.sequence),
                                    "audio": base64.b64encode(output.getvalue()).decode(
                                        "ascii"
                                    ),
                                }
                            )
            except (OSError, RuntimeError, ValueError):
                with self.lock:
                    self.audio_ready = False
                    self.error = "audio_capture_unavailable"
            finally:
                if self.capture and self.capture.poll() is None:
                    self.capture.terminate()
                    self.capture.wait(timeout=3)
            self.stop_event.wait(2)

    def next_utterance(self):
        self.lease_file.parent.mkdir(parents=True, exist_ok=True)
        self.lease_file.touch(mode=0o600, exist_ok=True)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not self.stop_event.is_set():
            with self.lock:
                if self.eligible() and self.queue:
                    item = self.queue.popleft()
                    self.pending[item["utterance_id"]] = item["call_id"]
                    while len(self.pending) > 8:
                        del self.pending[next(iter(self.pending))]
                    return {**self.snapshot(), "utterance": item}
                if not self.eligible():
                    return self.snapshot()
            self.stop_event.wait(0.05)
        return self.snapshot()

    def play(self, payload):
        key = (payload.get("call_id"), payload.get("utterance_id"))
        raw = base64.b64decode(payload.get("audio", ""), validate=True)
        if not 44 <= len(raw) <= 4 * 1024 * 1024:
            raise ValueError("invalid_audio_size")
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if audio.getnframes() / audio.getframerate() > 45:
                raise ValueError("reply_too_long")
        with self.lock:
            if key in self.played:
                return {"played": False, "reason": "already_attempted"}
            if (
                not self.eligible()
                or key[0] != self.call.get("inviteAt")
                or self.pending.get(key[1]) != key[0]
            ):
                return {"played": False, "reason": "stale_call_or_utterance"}
            if self.playback and self.playback.poll() is None:
                return {"played": False, "reason": "playback_busy"}
            self.played.append(key)
            del self.pending[key[1]]
            self.playback = subprocess.Popen(
                ["paplay", "--device=maibot_qq_mic", "--latency-msec=50"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
            )
            process = self.playback
        try:
            process.communicate(raw, timeout=50)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {"played": False, "reason": "playback_timeout"}
        with self.lock:
            completed = (
                process.returncode == 0
                and self.eligible()
                and key[0] == self.call.get("inviteAt")
            )
        return {
            "played": completed,
            "reason": "completed" if completed else "interrupted",
        }


def make_handler(gateway):
    """Bind authenticated handlers to this gateway instance.

    Args:
        gateway: Owned media process and call state.

    Returns:
        A handler class for the loopback server.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, code, data):
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(raw)

        def authorized(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + gateway.token
            ):
                self.respond(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/v1/heartbeat":
                gateway.lease_file.parent.mkdir(parents=True, exist_ok=True)
                gateway.lease_file.touch(mode=0o600, exist_ok=True)
                self.respond(200, {"ok": True})
            elif self.path == "/v1/status":
                self.respond(200, gateway.snapshot())
            elif self.path == "/v1/audio/next":
                self.respond(200, gateway.next_utterance())
            else:
                self.respond(404, {"error": "not_found"})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/audio/play" or not 0 < length <= 6 * 1024 * 1024:
                    self.respond(400, {"error": "invalid_request"})
                    return
                self.connection.settimeout(10)
                payload = json.loads(self.rfile.read(length))
                self.respond(200, gateway.play(payload))
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                wave.Error,
            ):
                self.respond(400, {"error": "invalid_audio_request"})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    token = Path(config["token_file"]).read_text().strip()
    if len(token) < 32 or not str(config["owner_user_id"]).isdigit():
        raise ValueError("invalid_gateway_identity_or_token")
    gateway = AudioGateway(
        token,
        str(config["owner_user_id"]),
        "http://127.0.0.1:6110",
        config["lease_file"],
        config["pulse_server"],
    )
    for target in (gateway.monitor, gateway.record):
        threading.Thread(target=target, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 6112), make_handler(gateway))
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        gateway.stop_event.set()
        for process in (gateway.capture, gateway.playback):
            if process and process.poll() is None:
                process.terminate()
        server.server_close()


if __name__ == "__main__":
    main()
