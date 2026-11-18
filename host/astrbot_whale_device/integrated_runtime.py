"""Own the services launched by the embedded AstrBot desktop window."""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

DASHBOARD_URL = 'http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_whale_device/whale'


class IntegratedRuntime:
    def __init__(self, config_path, *, startup_timeout=120):
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent.parent
        self.startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._service = None
        self._store = None
        self._process = None
        self._output = None
        self._session_secret = None
        self._token = None
        self._started = False

    @staticmethod
    def _port_open(port):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.3):
                return True
        except OSError:
            return False

    @staticmethod
    def _request(url, *, token=None, body=None, headers=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        outgoing = dict(headers or {})
        if token:
            outgoing['Authorization'] = 'Bearer ' + token
        if body is not None:
            outgoing['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=outgoing)
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=2) as response:
            return json.loads(response.read(1024 * 1024))

    def _dashboard_ready(self):
        try:
            result = self._request('http://127.0.0.1:6185/api/v1/auth/setup-status')
            return result.get('status') == 'ok' and isinstance(result.get('data', {}).get('setup_required'), bool)
        except Exception:
            return False

    def start(self):
        with self._lock:
            if self._started:
                return DASHBOARD_URL
            self._cancel.clear()
            try:
                from .gui_secrets import StoredSecrets
                from .host_config import HostConfig
                from .astrbot_native_bridge import ensure_rpc_token
                from .headless_service import HeadlessDeviceService
                from .desktop_device_transport import DesktopDeviceTransport
                from .storage import TaskStore
                from .qq_account import QQAccountController

                if not self.config_path.is_file():
                    raise RuntimeError('找不到本地配置，请先完成安装。')
                tunnel_settings = self.config_path.parent / "public-tunnel.json"
                if tunnel_settings.is_file():
                    os.environ["WHALE_PUBLIC_TUNNEL_CONFIG"] = str(tunnel_settings)
                config = HostConfig.load(self.config_path)
                credentials = StoredSecrets.load(self.config_path)
                if credentials.ensure_signing_secret():
                    credentials.save(self.config_path)
                credentials.apply_to_environ()
                token = ensure_rpc_token(str(self.config_path))
                if self._port_open(8767):
                    try:
                        state = self._request('http://127.0.0.1:8767/v1/device/status', token=token)
                        if not isinstance(state.get('started'), bool) or not state['started']:
                            raise ValueError('inactive')
                    except Exception:
                        raise RuntimeError('设备服务端口正被其他程序使用，请先关闭旧版主机。') from None
                else:
                    self._store = TaskStore(str(self.config_path.parent / 'runtime.sqlite3'))
                    self._service = HeadlessDeviceService.from_config(config, data_dir=self.config_path.parent, store=self._store)
                    self._service.host.plugin.transport = DesktopDeviceTransport(self._service.host, self.config_path)
                    self._service.start()
                if self._cancel.is_set():
                    raise RuntimeError('启动已取消。')
                if self._port_open(6185):
                    if not self._dashboard_ready():
                        raise RuntimeError('管理页面端口正被其他程序使用，请检查旧版 AstrBot。')
                else:
                    python = self.root / 'runtime/astrbot-venv/Scripts/python.exe'
                    launcher = self.root / 'tools/run-astrbot.py'
                    if not python.is_file() or not launcher.is_file():
                        raise RuntimeError('AstrBot 运行环境不完整，请重新安装。')
                    env = os.environ.copy()
                    self._session_secret = secrets.token_urlsafe(48)
                    env.update(ASTRBOT_DESKTOP_MANAGED='1', ASTRBOT_DESKTOP_SESSION_SECRET=self._session_secret)
                    log = self.root / 'runtime/logs/desktop-launcher.log'
                    log.parent.mkdir(parents=True, exist_ok=True)
                    self._output = log.open('a', encoding='utf-8')
                    self._process = subprocess.Popen([str(python), str(launcher)], cwd=self.root, env=env,
                        stdout=self._output, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    deadline = time.monotonic() + self.startup_timeout
                    while not self._dashboard_ready():
                        if self._cancel.wait(.25):
                            raise RuntimeError('启动已取消。')
                        if self._process.poll() is not None:
                            raise RuntimeError('AstrBot 启动失败，可在 runtime/logs 查看运行日志。')
                        if time.monotonic() >= deadline:
                            raise RuntimeError('AstrBot 启动超时，请稍后重试。')
                control = QQAccountController(self.config_path)
                if control.status().get('auto_login'):
                    control.clear_logout_suppression()
                    control.login()
                self._started = True
                return DASHBOARD_URL
            except Exception:
                self._cleanup()
                raise

    def dashboard_token(self):
        if self._token:
            return self._token
        if not self._session_secret or not self._process or self._process.poll() is not None:
            return None
        result = self._request('http://127.0.0.1:6185/api/v1/auth/desktop-session', body={},
            headers={'X-AstrBot-Desktop-Session': self._session_secret})
        token = result.get('data', {}).get('token')
        if result.get('status') != 'ok' or not isinstance(token, str) or not token:
            raise RuntimeError('桌面登录尚未就绪，请重试。')
        self._token = token
        return token

    def _cleanup(self):
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                process.terminate()
                process.wait(timeout=5)
        if self._output is not None:
            self._output.close()
            self._output = None
        try:
            if self._service is not None:
                self._service.stop()
        finally:
            self._service = None
            if self._store is not None:
                self._store.close()
                self._store = None
            self._started = False
            self._session_secret = self._token = None

    def stop(self):
        self._cancel.set()
        with self._lock:
            self._cleanup()
