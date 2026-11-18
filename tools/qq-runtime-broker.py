"""Own the desktop's NapCat process independently from AstrBot and the phone."""
from __future__ import annotations
import json, threading, time, uuid
from pathlib import Path


class QQRuntimeBroker:
    def __init__(self, config_path, service, connect_callback):
        self.config_path = Path(config_path).resolve()
        self.data_dir = self.config_path.parent
        self.service = service
        self.connect_callback = connect_callback
        self.state_path = self.data_dir/'qq-account-state.json'
        self.runtime_path = self.data_dir/'qq-account-runtime.json'
        self._stop = threading.Event()
        self._thread = None
        self._last_command = None
        self._want_login = False
        self._cached_login_attempted = False
        self._connected = False
        self._webui_client = None
        self._auth_retry_at = 0.0
        self._auth_backoff = 1.0
        self._service_retry_at = 0.0
        self._refresh_qr_requested = False

    def _state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8-sig'))
            return value if isinstance(value,dict) else {}
        except (ValueError,OSError):
            return {}

    def _runtime(self, **values):
        # No credentials, QR payloads, or exception text in the shared status.
        payload = {'broker_running':True,'updated_at':time.time(),**values}
        path = self.runtime_path.with_suffix('.broker.tmp')
        path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
        path.replace(self.runtime_path)

    def _save_last_login_account(self, account):
        if not account:
            return
        state = self._state()
        state["last_login_account"] = str(account)
        temporary = self.state_path.with_suffix('.broker.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
        temporary.replace(self.state_path)

    def _fallback_account(self):
        candidates = []
        config_dir = self.data_dir / 'qq-data' / 'NapCat' / 'config'
        for path in config_dir.glob('onebot11_*.json'):
            if path.stem[len('onebot11_'):].isdigit():
                candidates.append(path.stem[len('onebot11_'):])
        return candidates[0] if len(candidates) == 1 else None
    def _prepare_start(self):
        self._webui_client = None
        self._auth_retry_at = 0.0
        self._auth_backoff = 1.0
        """Let the EXE preference, rather than NapCat's separate preference, decide."""
        if hasattr(self.service,'snapshot') and hasattr(self.service,'prepare_install'):
            snapshot=self.service.snapshot()
            if not snapshot.get('installed') and not self.service._port_open():
                result=self.service.prepare_install(cancel_event=self._stop)
                if not result.get('ok'):raise RuntimeError('login_component_setup_failed')
        path = self.data_dir/'qq-data/NapCat/config/webui.json'
        if path.is_file() and not (self.data_dir/'qq-linux-runtime.json').exists():
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if value.get('autoLoginAccount'):
                self._save_last_login_account(value.get('autoLoginAccount'))
                value['autoLoginAccount']=''
                temporary=path.with_suffix('.broker.tmp')
                temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
                temporary.replace(path)
        if not self.service.start():
            raise RuntimeError('managed_login_service_start_failed')

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        from astrbot_whale_device.qq_account import QQAccountController
        control=QQAccountController(self.config_path)
        # Explicit login commands clear suppression; recovery preserves logout.
        state=self._state()
        self._want_login=bool(state.get('auto_login',False) or state.get('login_in_progress',False))
        self._last_command=state.get('command_id')
        if state.get('logout_requested'):
            self._want_login = False
        try:self._prepare_start()
        except Exception:self._runtime(logged_in=False,online=False,error='QQ 登录组件未就绪，点击登录后可重试。')
        self._thread=threading.Thread(target=self._run,name='whale-qq-account-broker',daemon=True)
        self._thread.start()

    def _client(self):
        if self._webui_client is not None:return self._webui_client
        from astrbot_whale_device.napcat_webui import NapCatWebUIClient
        now=time.monotonic()
        if now < self._auth_retry_at: raise RuntimeError("webui_auth_backoff")
        from astrbot_whale_device.linux_qq_service import login_endpoint
        endpoint, _ = login_endpoint(self.data_dir)
        client=NapCatWebUIClient(endpoint,timeout=3)
        try:
            client.authenticate(self.service.get_webui_token())
        except Exception:
            self._auth_retry_at=now+self._auth_backoff
            self._auth_backoff=min(self._auth_backoff*2.0,60.0)
            raise
        self._auth_backoff=1.0
        self._webui_client=client
        return client

    def _run(self):
        while not self._stop.is_set():
            state=self._state()
            command=state.get('command_id')
            if command and command!=self._last_command:
                self._last_command=command
                if state.get('logout_requested'):
                    self._want_login=False
                    self._cached_login_attempted=False
                    self._connected=False
                    self.service.stop()
                    self._webui_client=None
                    self._auth_retry_at=0.0
                    self._auth_backoff=1.0
                else:
                    self._want_login=True
                    self._refresh_qr_requested=True
                    self._cached_login_attempted=False
                    try:self._prepare_start()
                    except Exception:pass
            try:
                client=self._client()
                status=client.login_status()
                if status.get('logged_in'):
                    if state.get('logout_requested'):
                        # An externally owned process cannot be reported as offline.
                        self._runtime(logged_in=True,online=status.get('online',False),login_in_progress=False,
                                      account=status.get('account'),error='QQ 登录服务由外部程序启动，请在其控制台下线。')
                    else:
                        if not self._connected:
                            self.connect_callback()
                            self._connected=True
                        self._runtime(logged_in=True,online=status.get('online',False),login_in_progress=False,account=status.get('account'),error='')
                else:
                    self._connected=False
                    if self._refresh_qr_requested and hasattr(client, 'refresh_qrcode'):
                        client.refresh_qrcode()
                        self._refresh_qr_requested = False
                        self._cached_login_attempted = True
                    if self._want_login and not self._cached_login_attempted:
                        self._cached_login_attempted=True
                        accounts=client._request('/QQLogin/GetQuickLoginList')
                        account = str(accounts[0]) if isinstance(accounts,list) and len(accounts)==1 else None
                        if not account and isinstance(accounts,list) and len(accounts)==0:
                            account = self._fallback_account()
                        if account:
                            client._request('/QQLogin/SetQuickLogin',{'uin':account})
                        elif hasattr(client, 'refresh_qrcode'):
                            client.refresh_qrcode()
                    self._runtime(logged_in=False,online=False,login_in_progress=self._want_login,account=None,phase='login_required',error='')
            except Exception:
                if self._want_login and not state.get('logout_requested') and time.monotonic() >= self._service_retry_at:
                    self._service_retry_at = time.monotonic() + 15
                    try:
                        self._prepare_start()
                    except Exception:
                        pass
                if self._webui_client is not None:
                    self._webui_client=None
                    self._auth_retry_at=time.monotonic()+self._auth_backoff
                    self._auth_backoff=min(self._auth_backoff*2.0,60.0)
                self._runtime(logged_in=False,online=False,login_in_progress=False,account=None,phase='service_unavailable',
                              error='' if state.get('logout_requested') else '正在等待 QQ 登录服务。')
            self._stop.wait(1.5)

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():self._thread.join(timeout=6)
        self.service.stop()
        self._runtime(broker_running=False,logged_in=False,online=False,login_in_progress=False,account=None)
