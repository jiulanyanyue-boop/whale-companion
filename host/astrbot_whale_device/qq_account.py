"""Thread-safe shared QQ account command and runtime state."""
from __future__ import annotations
import json
import threading
import uuid
import time
from pathlib import Path
from typing import Callable, Any

class QQAccountController:
    def __init__(self, config_path, *, start_login: Callable[[], Any] | None = None,
                 stop_login: Callable[[], Any] | None = None,
                 status_provider: Callable[[], dict] | None = None):
        self.path = Path(config_path).resolve().parent / "qq-account-state.json"
        self.runtime_path = self.path.with_name("qq-account-runtime.json")
        self._lock = threading.RLock()
        self._start_login, self._stop_login = start_login, stop_login
        self._status_provider = status_provider
        self._webui_client = None
        self._auth_retry_at = 0.0
        self._auth_backoff = 1.0
        self._qr_refreshed_at = 0.0

    def _read(self, path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def status(self) -> dict:
        with self._lock:
            result = self._read(self.path)
            result.setdefault("auto_login", False)
            result.setdefault("logout_requested", False)
            result.setdefault("login_in_progress", False)
            result.setdefault("account", None)
            runtime=self._read(self.runtime_path)
            if runtime.get('updated_at') and time.time()-runtime['updated_at']>15:
                runtime.update(broker_running=False,online=False,logged_in=False,login_in_progress=False,phase='service_unavailable',error='QQ 登录服务连接已断开，请重新登录。')
            result.update(runtime)
            if result.get('broker_running') is False:
                result['login_in_progress'] = False
                result['online'] = result['logged_in'] = False
            result['login_required'] = not result.get('logout_requested') and not result.get('online') and bool(
                result.get('broker_running') is False or result.get('login_in_progress') or result.get('phase') == 'login_required'
            )
            if self._status_provider:
                try:
                    external = self._status_provider() or {}
                    if isinstance(external, dict):
                        result.update({k: v for k, v in external.items() if k not in ("qr_url", "qrcodeurl")})
                except Exception:
                    pass
            return result

    def _command(self, **values):
        state = self._read(self.path)
        state.update(values)
        state["command_id"] = uuid.uuid4().hex
        self._write(self.path, state)
        return self.status()

    def set_auto_login(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("auto_login 必须是布尔值")
        with self._lock:
            state = self._read(self.path)
            state["auto_login"] = enabled
            self._write(self.path, state)
            return self.status()

    def update_runtime(self, **values) -> dict:
        allowed = {"broker_running", "logged_in", "online", "error", "login_in_progress", "account", "phase"}
        with self._lock:
            runtime = self._read(self.runtime_path)
            runtime.update({k: v for k, v in values.items() if k in allowed})
            self._write(self.runtime_path, runtime)
            return self.status()

    def login(self):
        with self._lock:
            self._webui_client=None
            self._auth_retry_at=0.0
            self._auth_backoff=1.0
            self._qr_refreshed_at=0.0
            result = self._command(logout_requested=False, login_in_progress=True)
        if self._start_login:
            self._start_login()
        return result

    def cancel_login(self):
        with self._lock:
            self._webui_client=None
            self._auth_retry_at=0.0
            self._auth_backoff=1.0
            result = self._command(logout_requested=True, login_in_progress=False)
        if self._stop_login:
            self._stop_login()
        return result

    logout = cancel_login

    def clear_logout_suppression(self):
        with self._lock:
            state = self._read(self.path)
            state["logout_requested"] = False
            self._write(self.path, state)
            return self.status()

    def login_qr(self):
        """Sensitive payload: serve only through the authenticated dashboard."""
        import base64,io
        import qrcode
        from .napcat_webui import NapCatWebUIClient
        if self.status().get('logout_requested'):return {'available':False}
        if self._webui_client is None:
            if time.monotonic() < self._auth_retry_at:return {'available':False}
            from .linux_qq_service import login_endpoint, login_token
            endpoint, token_path = login_endpoint(self.path.parent)
            token=login_token(token_path)
            client=NapCatWebUIClient(endpoint,timeout=3)
            try:
                client.authenticate(token)
            except Exception:
                self._auth_retry_at=time.monotonic()+self._auth_backoff
                self._auth_backoff=min(self._auth_backoff*2.0,60.0)
                return {'available':False}
            self._webui_client=client
            self._auth_backoff=1.0
        try:
            status=self._webui_client.login_status()
        except Exception:
            self._webui_client=None
            self._auth_retry_at=time.monotonic()+self._auth_backoff
            self._auth_backoff=min(self._auth_backoff*2.0,60.0)
            return {'available':False}
        if status.get('logged_in'):return {'available':False,'logged_in':True}
        if time.monotonic() - self._qr_refreshed_at > 90 and hasattr(self._webui_client, 'refresh_qrcode'):
            self._webui_client.refresh_qrcode()
            self._qr_refreshed_at = time.monotonic()
            status = self._webui_client.login_status()
        url=status.get('qr_url')
        if not url:return {'available':False}
        data=io.BytesIO();qrcode.make(url).save(data,format='PNG')
        return {'available':True,'qr_image':'data:image/png;base64,'+base64.b64encode(data.getvalue()).decode()}

__all__ = ["QQAccountController"]
