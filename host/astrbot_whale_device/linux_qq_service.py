"""Manage the WSL QQ login and relay its OneBot connection to AstrBot."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path

from .napcat_webui import NapCatWebUIClient


def login_endpoint(data_dir):
    """Resolve the selected runtime's endpoint and credential file.

    Args:
        data_dir: Desktop configuration directory.

    Returns:
        Local WebUI address and token path for the selected runtime.
    """
    root = Path(data_dir)
    marker = root / "qq-linux-runtime.json"
    if (
        marker.exists()
        and json.loads(marker.read_text(encoding="utf-8")).get("enabled") is True
    ):
        return "http://127.0.0.1:6109", root / "qq-linux-webui.token"
    from .qq_settings import QQSettings

    return QQSettings.load(
        root / "host.json"
    ).webui_url, root / "qq-data/NapCat/config/webui.json"


def login_token(path):
    value = Path(path).read_text(encoding="utf-8-sig")
    return json.loads(value)["token"] if Path(path).suffix == ".json" else value.strip()


class LinuxQQService:
    """Expose the existing broker contract for one Linux QQ identity."""

    def __init__(self, project):
        self.project = Path(project)
        self.data_dir = self.project / "dist-exe"
        self._stop = threading.Event()
        self._relay = None
        self._account = None

    def _manage(self, action):
        import sys

        subprocess.run(
            [sys.executable, str(self.project / "tools/qq-call/manage.py"), action],
            capture_output=True,
            check=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def start(self):
        self._stop.clear()
        self._manage("start")
        return True

    def stop(self):
        self._stop.set()
        self._manage("stop")

    def get_webui_token(self):
        return login_token(login_endpoint(self.data_dir)[1])

    def connect(self):
        client = NapCatWebUIClient("http://127.0.0.1:6109")
        client.authenticate(self.get_webui_token())
        status = client.login_status()
        if not status.get("logged_in"):
            raise RuntimeError("QQ_login_required")
        self._account = str(status["account"])
        if self._relay and self._relay.is_alive():
            return
        self._stop.clear()
        self._relay = threading.Thread(
            target=lambda: asyncio.run(self._forward()),
            daemon=True,
            name="whale-linux-onebot",
        )
        self._relay.start()

    async def _forward(self):
        import websockets

        token = (self.data_dir / "qq-linux-onebot.token").read_text().strip()
        config = json.loads(
            (self.project / "runtime/astrbot/data/cmd_config.json").read_text(
                encoding="utf-8-sig"
            )
        )
        platform = next(p for p in config["platform"] if p["id"] == "whale_qq")

        async def copy(source, target):
            async for message in source:
                await target.send(message)

        while not self._stop.is_set():
            try:
                async with (
                    websockets.connect(
                        "ws://127.0.0.1:6113",
                        additional_headers={"Authorization": "Bearer " + token},
                        proxy=None,
                    ) as linux,
                    websockets.connect(
                        "ws://127.0.0.1:6199/ws",
                        additional_headers={
                            "Authorization": "Bearer " + platform["ws_reverse_token"],
                            "X-Self-ID": self._account,
                            "X-Client-Role": "Universal",
                        },
                        proxy=None,
                    ) as astrbot,
                ):
                    tasks = [
                        asyncio.create_task(copy(linux, astrbot)),
                        asyncio.create_task(copy(astrbot, linux)),
                    ]
                    while not self._stop.is_set() and not any(
                        task.done() for task in tasks
                    ):
                        await asyncio.sleep(0.25)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            except (OSError, ValueError, websockets.WebSocketException):
                await asyncio.sleep(2)
