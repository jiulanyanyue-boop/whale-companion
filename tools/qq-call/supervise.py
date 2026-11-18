"""Supervise only the isolated Linux QQ call runtime's own processes."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    bridge = ROOT / "bridge"
    config = json.loads((ROOT / "gateway.json").read_text())
    lock_file = (ROOT / "supervisor.lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Call runtime already running.")
    (ROOT / "supervisor.pid").write_text(str(os.getpid()))
    env = {
        **os.environ,
        "WHALE_QQ_CALL_OWNER_UIN": config["owner_user_id"],
        "WHALE_QQ_CALL_LEASE_FILE": config["lease_file"],
        "NAPCAT_WORKDIR": str(ROOT / "qq-package/opt/QQ/resources/app/napcat"),
        "NAPCAT_WEBUI_PREFERRED_PORT": "6109",
    }
    children = []
    logs = []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for name, cmd in [
            ("qq", ["bash", str(bridge / "scripts/run-napcat.sh")]),
            (
                "gateway",
                [
                    sys.executable,
                    str(ROOT / "gateway.py"),
                    "--config",
                    str(ROOT / "gateway.json"),
                ],
            ),
        ]:
            log = (bridge / f"logs/{name}.log").open("ab")
            logs.append(log)
            children.append(
                subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        while all(child.poll() is None for child in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
        for log in logs:
            log.close()
        (ROOT / "supervisor.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
