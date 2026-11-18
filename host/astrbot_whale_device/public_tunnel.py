"""Keep the phone's existing TLS services reachable through an SSH relay."""

import json
import subprocess
import threading
from pathlib import Path


class PublicTunnel:
    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self.stopped = threading.Event()
        self.process = None

    def start(self):
        if not self.settings_path.is_file():
            return
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        if not settings.get("enabled"):
            return
        self.thread = threading.Thread(target=self._run, args=(settings,), daemon=True)
        self.thread.start()

    def _run(self, settings):
        command = [
            "ssh",
            "-N",
            "-T",
            "-i",
            str(Path(settings["identity_file"]).expanduser()),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=12",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        for remote, local in settings["ports"]:
            command += ["-R", f"127.0.0.1:{int(remote)}:127.0.0.1:{int(local)}"]
        command.append(settings["ssh_target"])
        while not self.stopped.is_set():
            try:
                self.process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                while self.process.poll() is None:
                    if self.stopped.wait(1):
                        self.process.terminate()
                        self.process.wait(timeout=10)
                        return
            except (OSError, subprocess.SubprocessError):
                pass
            self.stopped.wait(5)

    def stop(self):
        self.stopped.set()
        if hasattr(self, "thread"):
            self.thread.join(timeout=15)


if __name__ == "__main__":
    import sys

    tunnel = PublicTunnel(sys.argv[1])
    tunnel.start()
    try:
        tunnel.thread.join()
    except KeyboardInterrupt:
        tunnel.stop()
