"""Background coordinator for page-local NapCat preparation and QQ QR login.

Only poll_events() crosses into the UI. No worker touches Tk widgets, and a
cancelled generation cannot reconnect or publish a stale login QR code.
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .napcat_service import NapCatService
from .napcat_webui import NapCatWebUIClient, NapCatWebUIError, NapCatTokenError, NapCatTwoFactorRequired
from .qq_settings import QQSettings


class OnboardingError(RuntimeError):
    pass


class QQOnboarding:
    def __init__(self, config_path, *, service=None, client_factory=NapCatWebUIClient,
                 probe=None, poll_interval=1.5, ready_timeout=60):
        self.service = service if service is not None else NapCatService(Path(config_path).resolve().parent)
        self._client_factory = client_factory
        self._probe = probe or self._port_ready
        self._interval = max(.02, poll_interval)
        self._ready_timeout = ready_timeout
        self._lock = threading.RLock()
        self._thread = None
        self._generation = 0
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._events = queue.Queue(maxsize=300)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    @staticmethod
    def _port_ready(url):
        address = urlsplit(url)
        try:
            with socket.create_connection((address.hostname, address.port or (443 if address.scheme == 'https' else 80)), timeout=.4):
                return True
        except OSError:
            return False

    def _publish(self, generation, event):
        with self._lock:
            if generation != self._generation or self._stop.is_set():
                return
            try:
                self._events.put_nowait(event)
            except queue.Full:
                self._events.get_nowait()
                self._events.put_nowait(event)

    def _active(self, generation, stop):
        return not stop.is_set() and generation == self._generation

    def start(self, *, webui_url, webui_token='', onebot_url='ws://127.0.0.1:3001',
              onebot_token='', launcher_path='', qq_path='', totp_code=''):
        QQSettings(ws_url=onebot_url, webui_url=webui_url, launcher_path=launcher_path, qq_path=qq_path)
        address = urlsplit(onebot_url)
        if address.scheme != 'ws' or address.hostname not in ('127.0.0.1', 'localhost', '::1') or address.path not in ('', '/'):
            raise OnboardingError('自动登录需要本机消息地址，例如 ws://127.0.0.1:3001；其它地址请使用高级设置手动接入。')
        with self._lock:
            if self.running:
                raise OnboardingError('上一段登录流程仍在结束，请稍后重试。')
            self._generation += 1
            generation = self._generation
            self._stop = threading.Event()
            self._refresh = threading.Event()
            self._events = queue.Queue(maxsize=300)
            self._thread = threading.Thread(target=self._run, args=(generation, self._stop, self._refresh,
                webui_url, webui_token, address.port or 3001, onebot_token, launcher_path, qq_path, totp_code),
                name='whale-qq-login', daemon=True)
            self._thread.start()

    def refresh_qrcode(self):
        self._refresh.set()

    def stop(self, *, stop_service=False):
        with self._lock:
            self._generation += 1
            self._stop.set()
            while not self._events.empty():
                try: self._events.get_nowait()
                except queue.Empty: break
        if stop_service:
            self.service.stop()

    def poll_events(self):
        result = []
        for item in self.service.poll_events():
            result.append({'type': 'log', 'message': '[登录服务] ' + item.get('message', '')})
        for _ in range(300):
            try: result.append(self._events.get_nowait())
            except queue.Empty: break
        return result

    def _run(self, generation, stop, refresh, webui_url, webui_token, port, onebot_token, launcher_path, qq_path, totp_code):
        publish = lambda event: self._publish(generation, event)
        def phase(name, detail):
            publish({'type': 'phase', 'phase': name, 'detail': detail})
        try:
            phase('preparing', '正在检查本机登录服务。首次使用会自动准备官方登录组件。')
            if not self._probe(webui_url):
                if (urlsplit(webui_url).port or 80) != 6099:
                    raise OnboardingError('指定的登录服务尚未运行。请启动已有服务，或把登录服务地址改回 http://127.0.0.1:6099。')
                snapshot = self.service.snapshot()
                if not launcher_path and not snapshot['installed']:
                    last_percent = [-1]
                    def progress(event):
                        total = event.get('total', 0)
                        done = event.get('downloaded', 0)
                        percent = int(100 * done / total) if total else 0
                        if percent != last_percent[0]:
                            last_percent[0] = percent
                            component = '登录内核' if 'Windows.Node' in event.get('asset', '') else '登录界面'
                            publish({'type': 'progress', 'detail': f'正在准备{component}：{percent}%' if total else '正在下载登录组件…'})
                    installed = self.service.prepare_install(progress=progress, cancel_event=stop)
                    if not self._active(generation, stop): return
                    if not installed.get('ok'):
                        raise OnboardingError('登录组件准备失败，请查看本页运行日志；也可在高级设置选择已有 NapCat。')
                if not self._active(generation, stop): return
                phase('starting', '正在后台启动登录服务，过程会记录在本页运行日志。')
                with self._lock:
                    if not self._active(generation, stop): return
                    if not self.service.start(launcher_path=launcher_path or None, qq_path=qq_path or None):
                        raise OnboardingError(self.service.snapshot().get('error') or '登录服务启动失败，请查看本页运行日志。')
                deadline = time.monotonic() + self._ready_timeout
                while not self._probe(webui_url):
                    if not self._active(generation, stop): return
                    if time.monotonic() > deadline:
                        raise OnboardingError('登录服务未能就绪。请查看本页运行日志，确认桌面 QQ 与登录组件可用。')
                    stop.wait(.3)
            if not self._active(generation, stop): return
            token = webui_token or self.service.get_webui_token()
            if not token:
                raise NapCatTokenError('已有登录服务需要管理 Token，请在本页高级设置填写后重试。')
            client = self._client_factory(webui_url)
            client.authenticate(token, totp_code=totp_code or None)
            configured = False
            last_phase = None
            last_qr = ''
            blank_qr_since = time.monotonic()
            refreshed_missing = False
            failures = 0
            while self._active(generation, stop):
                try:
                    if refresh.is_set():
                        refresh.clear()
                        result = client.refresh_qrcode()
                        if not self._active(generation, stop): return
                        if result.get('qr_url'):
                            last_qr = result['qr_url']
                            publish({'type': 'qrcode', 'url': last_qr})
                    status = client.login_status()
                    if not self._active(generation, stop): return
                    if status.get('logged_in') and status.get('core_ready'):
                        if not configured:
                            phase('configuring', '登录已完成，正在自动配置消息连接。无需手动填写地址或 Token。')
                            connection = client.ensure_websocket(port=port, onebot_token=onebot_token)
                            if not self._active(generation, stop): return
                            configured = True
                            last_phase = 'configured'
                            publish({'type': 'configured', 'ws_url': f"ws://127.0.0.1:{connection['port']}"})
                            publish({'type': 'log', 'message': '助手已完成扫码登录，正在验证消息连接。'})
                    elif not status.get('logged_in'):
                        configured = False
                        name = status.get('login_phase') or 'waiting_qrcode'
                        if name not in ('qrcode_scanned', 'initializing'):
                            name = 'waiting_qrcode'
                        detail = '二维码已被扫描，请在手机 QQ 上确认登录。' if name == 'qrcode_scanned' else '用手机 QQ 扫描左侧二维码，然后在手机上确认登录。'
                        if '过期' in str(status.get('login_error') or ''):
                            detail = '二维码已过期，请点击“刷新二维码”后重新扫码。'
                        if (name, detail) != last_phase:
                            phase(name, detail)
                            last_phase = (name, detail)
                        qr = status.get('qr_url')
                        if isinstance(qr, str) and 0 < len(qr) <= 4096 and qr != last_qr:
                            publish({'type': 'qrcode', 'url': qr})
                            last_qr = qr
                        if not qr and not refreshed_missing and time.monotonic() - blank_qr_since > 3:
                            client.refresh_qrcode()
                            refreshed_missing = True
                    else:
                        if last_phase != 'initializing':
                            phase('initializing', '扫码已确认，正在等待 QQ 初始化完成。')
                            last_phase = 'initializing'
                    failures = 0
                    stop.wait(self._interval)
                except (NapCatTokenError, NapCatTwoFactorRequired):
                    raise
                except NapCatWebUIError:
                    failures += 1
                    if failures >= 4:
                        raise OnboardingError('登录服务暂时无法读取，请查看本页运行日志后重试。') from None
                    stop.wait(min(failures, 3))
        except (NapCatTokenError, NapCatTwoFactorRequired) as exc:
            publish({'type': 'error', 'message': str(exc), 'auth_required': True})
        except (OnboardingError, NapCatWebUIError) as exc:
            publish({'type': 'error', 'message': str(exc)})
        except Exception:
            publish({'type': 'error', 'message': 'QQ 登录流程未能完成，请查看本页运行日志后重试。'})
