"""Exercise virtual audio capture/playback without a QQ call or network request."""

import array
import os
import subprocess
import threading
import time
from pathlib import Path

from gateway import AudioGateway


def main():
    root = Path.home() / ".local/share/whale-qq-call"
    pulse = "unix:" + str(root / "bridge/runtime/pulse/native")
    gate = AudioGateway(
        "local-probe-unused", "1", "http://127.0.0.1:6110", root / "probe.lease", pulse
    )
    # This state exists only in this test instance, never in the running gateway.
    gate.call = {
        "phase": "connected",
        "inviteAt": "local-audio-probe",
        "callerUin": "1",
    }
    worker = threading.Thread(target=gate.record, daemon=True)
    worker.start()
    env = {**os.environ, "PULSE_SERVER": pulse}
    capture = None
    try:
        source = (
            Path(__file__).resolve().parents[2]
            / "runtime/astrbot/data/plugins/astrbot_plugin_whale_device/voice_assets/test_goodbye.wav"
        )
        time.sleep(0.5)
        subprocess.run(
            ["paplay", "--device=maibot_qq_speaker", str(source)],
            env=env,
            check=True,
            timeout=15,
        )
        deadline = time.monotonic() + 5
        while not gate.queue and time.monotonic() < deadline:
            time.sleep(0.1)
        assert gate.queue, "No utterance captured through the virtual speaker."
        item = gate.queue.popleft()
        gate.pending[item["utterance_id"]] = item["call_id"]
        capture = subprocess.Popen(
            [
                "parec",
                "--raw",
                "--device=maibot_qq_mic.monitor",
                "--format=s16le",
                "--rate=16000",
                "--channels=1",
                "--latency-msec=20",
            ],
            stdout=subprocess.PIPE,
            env=env,
        )
        captured = []

        def read_audio():
            while block := capture.stdout.read(640):
                captured.append(block)

        reader = threading.Thread(target=read_audio, daemon=True)
        reader.start()
        time.sleep(0.2)
        receipt = gate.play(item)
        assert receipt["played"], receipt
        time.sleep(0.2)
        capture.terminate()
        reader.join(timeout=2)
        samples = array.array("h", b"".join(captured))
        assert any(abs(x) > 500 for x in samples), (
            "No audio reached the virtual QQ microphone."
        )
        print(
            "PASS: virtual speaker -> utterance WAV -> virtual microphone; no QQ call placed."
        )
    finally:
        gate.stop_event.set()
        for process in (gate.capture, gate.playback, capture):
            if process and process.poll() is None:
                process.terminate()
        worker.join(timeout=3)


if __name__ == "__main__":
    main()
