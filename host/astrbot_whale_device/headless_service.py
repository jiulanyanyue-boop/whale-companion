"""Framework-neutral lifecycle facade for AstrBot and other headless hosts.

This module deliberately imports no Tk/desktop modules.  It owns the three
local services that used to be assembled by ``host_gui``: device WSS,
pairing HTTP, and authenticated loopback RPC.  The supplied ``host`` object
is expected to expose ``config``, ``device_server``, ``start`` and ``stop``
(``StandaloneHost`` satisfies this contract).
"""
from __future__ import annotations

import json
import os
import socket
import ssl
from pathlib import Path
from typing import Any

from .astrbot_native_bridge import AstrBotDeviceRPC, ensure_rpc_token
from .pairing_endpoint import (
    PairingEndpoint,
    build_pairing_service,
    make_host_identity_from_seed,
    start_pairing_server,
)


def _port_open(host: str, port: int, timeout: float = 0.15) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class HeadlessDeviceService:
    """Start/stop device services without importing the desktop UI.

    ``data_dir`` is the plugin-owned persistent directory.  No path is
    inferred from the repository or ``dist-exe``. An occupied RPC port fails
    startup rather than mixing credentials with another service.
    """

    def __init__(
        self,
        host: Any,
        *,
        data_dir: str | os.PathLike[str],
        rpc_port: int = 8767,
        rpc_token_path: str | os.PathLike[str] | None = None,
        pairing_port: int | None = None,
    ) -> None:
        self.host = host
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.rpc_port = int(rpc_port)
        self.pairing_port = pairing_port
        self.rpc_token_path = Path(rpc_token_path).resolve() if rpc_token_path else self.data_dir / "rpc-secret.json"
        self.pairing_server = None
        self.pairing_thread = None
        self.rpc_server: AstrBotDeviceRPC | None = None
        self.rpc_external = False
        self.started = False
        self.actual_device_port = 0
        self.actual_pairing_port = 0
        self.host_identity = None
        self._pairing_service = None
        # Read-only RPC handlers discover the facade through the supplied host.
        setattr(host, "headless_service", self)

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        data_dir: str | os.PathLike[str],
        environ: dict[str, str] | None = None,
        store: Any = None,
        **kwargs: Any,
    ) -> "HeadlessDeviceService":
        """Build a headless service from HostConfig without importing desktop UI."""
        from .standalone import StandaloneHost
        host = StandaloneHost(config, environ=environ, store=store)
        return cls(host, data_dir=data_dir, **kwargs)

    @property
    def config(self) -> Any:
        return self.host.config

    def _pairing_ssl(self) -> ssl.SSLContext | None:
        cfg = self.config.device_server
        if not cfg.uses_tls:
            host = str(cfg.host or "127.0.0.1").lower()
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("plaintext pairing HTTP is forbidden outside loopback; configure TLS")
            return None
        missing = cfg.missing_tls_files()
        if missing:
            raise FileNotFoundError(f"TLS files declared but missing: {missing}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cfg.certfile, cfg.keyfile)
        return context

    def start(self) -> dict[str, Any]:
        if self.started:
            return self.status()
        if _port_open("127.0.0.1", self.rpc_port):
            raise RuntimeError("设备桥接端口已被占用；请使用已有桌面桥接或停止另一份插件。")
        try:
            self.actual_device_port = int(self.host.start())
            secret = os.environ.get(self.config.signing_secret_env, "").encode("utf-8")
            self.host_identity = make_host_identity_from_seed(secret)
            pairing_db = self.data_dir / "pairing.sqlite3"
            self._pairing_service = build_pairing_service(str(pairing_db), host_identity=self.host_identity)
            port = self.pairing_port
            if port is None:
                configured = int(self.config.device_server.port or 0)
                port = configured + 1 if configured else 0
            self.pairing_server, self.pairing_thread, self.actual_pairing_port = start_pairing_server(
                host=self.config.device_server.host,
                port=port,
                service=self._pairing_service,
                host_identity=self.host_identity,
                ssl_context=self._pairing_ssl(),
            )
            if _port_open("127.0.0.1", self.rpc_port):
                raise RuntimeError("设备桥接端口已被其他服务占用。")
            token = ensure_rpc_token(str(self.rpc_token_path.parent / ".rpc-config.json"))
            self.rpc_server = AstrBotDeviceRPC(self.host, token, port=self.rpc_port, config_path=self.data_dir/'host.json')
            self.rpc_server.start()
            self.started = True
            return self.status()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        rpc, pairing, host = self.rpc_server, self.pairing_server, self.host
        self.rpc_server = None
        self.pairing_server = None
        self.pairing_thread = None
        self.started = False
        if rpc is not None:
            rpc.stop()
        if pairing is not None:
            pairing.shutdown()
            pairing.server_close()
        if self._pairing_service is not None:
            self._pairing_service.tokens.db.close()
            self._pairing_service=None
        if getattr(host, "device_server", None) is not None and host.device_server.is_running:
            host.stop()

    def status(self) -> dict[str, Any]:
        server = getattr(self.host, "device_server", None)
        connections = []
        if server is not None:
            for item in server.connections():
                connections.append(item.as_dict())
        return {
            "started": bool(self.started),
            "device_url": getattr(server, "url", "") if server is not None else "",
            "device_port": self.actual_device_port,
            "pairing_port": self.actual_pairing_port,
            "pairing_url": f"{'https' if self._pairing_ssl() else 'http'}://{self.config.device_server.host}:{self.actual_pairing_port}/pair/" if self.actual_pairing_port else "",
            "rpc_port": self.rpc_port,
            "rpc_external": self.rpc_external,
            "connections": connections,
        }

    def pairing(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Call a pairing endpoint action without exposing tokens in logs."""
        if self._pairing_service is None:
            raise RuntimeError("headless service is not started")
        endpoint = PairingEndpoint(self._pairing_service, host_identity=self.host_identity)
        if action == "status":
            return endpoint.status(kwargs.get("token"), kwargs.get("device_id"))
        if action == "hostinfo":
            return {"ok": True, "host_fingerprint": endpoint.host_fingerprint(), "host_identity_is_placeholder": not endpoint.has_real_identity()}
        method = getattr(endpoint, action, None)
        if method is None or action not in {"register", "confirm", "finalize"}:
            raise ValueError(f"unknown pairing action: {action}")
        return method(kwargs.get("body", kwargs))

    def apps(self) -> dict[str, Any]:
        """Return read-only device/app view; app inventory is device-owned."""
        return {"devices": self.status()["connections"], "apps": []}


def create_headless_service(data_dir, settings):
    """First-run plugin setup. Chat providers remain entirely owned by AstrBot."""
    import secrets
    from .host_config import HostConfig
    from .tls_cert import ensure_self_signed_certificate
    from .gui_secrets import _tighten_permissions
    from .storage import TaskStore
    from .desktop_device_transport import DesktopDeviceTransport
    root=Path(data_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    owner=str(settings.get('owner_user_id','')).strip()
    if not owner or not owner.isdigit():raise ValueError('请先在插件设置中填写主人 QQ 号。')
    path=root/'host.json'
    secret_path=root/'phone-signing-secret.json'
    if not secret_path.exists():
        secret_path.write_text(json.dumps({'secret':secrets.token_hex(32)}),encoding='utf-8');_tighten_permissions(secret_path)
    signing=json.loads(secret_path.read_text(encoding='utf-8'))['secret']
    os.environ['WHALE_PLUGIN_SIGNING_SECRET']=signing
    cert=root/'phone-cert.pem';key=root/'phone-key.pem'
    ensure_self_signed_certificate(certfile=cert,keyfile=key)
    raw=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
        'owner_user_id':owner,'device_id':str(settings.get('device_id') or 'whale-phone'),
        'signing_secret_env':'WHALE_PLUGIN_SIGNING_SECRET','model_api_key_env':'',
        'model':{'provider_id':'openai','model_id':'astrbot-managed','base_url':'https://api.openai.com'},
        'allowed_packages':[], 'blocked_packages':[],
        'device_server':{'host':str(settings.get('device_listen_host') or '0.0.0.0'),'port':int(settings.get('device_listen_port',8765)),
                         'certfile':str(cert),'keyfile':str(key)}}
    raw['owner_user_id']=owner
    raw['device_server'].update(certfile=str(cert),keyfile=str(key))
    raw['device_server']['public_url'] = str(settings.get('device_public_url', raw['device_server'].get('public_url', ''))).strip()
    config=HostConfig.from_mapping(raw)
    path.write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
    service=HeadlessDeviceService.from_config(config,data_dir=root,store=TaskStore(str(root/'runtime.sqlite3')),
                                            rpc_port=int(settings.get('device_rpc_port',8767)),pairing_port=int(settings.get('pairing_port',8766)))
    service.host.plugin.transport=DesktopDeviceTransport(service.host,path)
    return service



