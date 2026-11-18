"""Authenticated loopback RPC exposing only validated device plans to AstrBot."""
from __future__ import annotations
import hmac, json, os, secrets, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from .validation import PlanValidationError, validate_device_plan

RPC_HOST = "127.0.0.1"
RPC_PORT = 8767
RPC_TOKEN_ENV = "WHALE_DEVICE_RPC_TOKEN"

# Group capabilities are selected by the AstrBot plugin; keep the bridge's
# independent enforcement aligned with that social-safe command surface.
GROUP_COMMANDS = frozenset({
    "device.device_status",
    "device.gallery.list",
    "device.gallery.read",
    "device.app.list",
    "device.app.launch",
    "device.ui.observe",
    "device.screen.capture",
    "device.ui.scroll",
    "device.ui.back",
    "device.ui.home",
    "device.ui.recents",
})

def ensure_rpc_token(config_path: str) -> str:
    """Load or mint the local bridge credential without logging its value."""
    from pathlib import Path
    from .gui_secrets import _tighten_permissions
    path = Path(config_path).resolve().parent / "device-rpc-secret.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = str(payload.get("token", "")).strip() if isinstance(payload, dict) else ""
    except (OSError, ValueError):
        token = ""
    if len(token) < 32:
        token = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"token": token}), encoding="utf-8")
        _tighten_permissions(path)
    os.environ[RPC_TOKEN_ENV] = token
    return token

class AstrBotDeviceRPC:
    def __init__(self, host: Any, token: str, bind_host: str = RPC_HOST, port: int = RPC_PORT, config_path=None):
        if bind_host != RPC_HOST:
            raise ValueError("AstrBot RPC must bind to 127.0.0.1")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("RPC token must contain at least 32 characters")
        self.host, self.token = host, token
        self.management = None
        if config_path is not None:
            from .device_management import DeviceManagement
            self.management = DeviceManagement(host, config_path)
        self.stopping = threading.Event()
        self.server = ThreadingHTTPServer((bind_host, port), self._make_handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="astrbot-device-rpc", daemon=True)

    def _make_handler(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return
            def _reply(self, status: int, payload: dict[str, Any]):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def _authorized(self) -> bool:
                return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {owner.token}")
            def do_GET(self):
                if not self._authorized():
                    self._reply(401, {"error": "unauthorized"}); return
                route, _, query = self.path.partition("?")
                if owner.management is not None and route in {"/v1/device/status", "/v1/device/apps", "/v1/device/pairing", "/v1/device/connection"}:
                    try:
                        method={"/v1/device/status":"status","/v1/device/apps":"allowed_apps","/v1/device/pairing":"pairing_info","/v1/device/connection":"connection_settings"}[route]
                        self._reply(200, getattr(owner.management,method)())
                    except Exception as exc:
                        self._reply(503, {"error": f"device_management:{type(exc).__name__}"})
                    return
                service = getattr(owner.host, "headless_service", None)
                if route == "/v1/device/status":
                    if service is not None:
                        self._reply(200, service.status())
                    else:
                        server = getattr(owner.host, "device_server", None)
                        rows = [item.as_dict() for item in server.connections()] if server is not None else []
                        self._reply(200, {"started": bool(server and server.is_running), "connections": rows})
                    return
                if route == "/v1/device/apps":
                    self._reply(200, service.apps() if service is not None else {"devices": [], "apps": []}); return
                if route == "/v1/device/pairing/status":
                    device_id = ""
                    for item in query.split("&"):
                        key, _, value = item.partition("=")
                        if key == "device_id": device_id = value
                    try:
                        payload = service.pairing("status", device_id=device_id) if service is not None else {"ok": False, "message": "pairing unavailable"}
                        self._reply(200, payload)
                    except Exception as exc:
                        self._reply(503, {"error": f"host_error:{exc.__class__.__name__}"})
                    return
                self._reply(404, {"error": "not_found"})
            def do_POST(self):
                if self.path == "/v1/device/connection":
                    if not self._authorized():
                        self._reply(401, {"error": "unauthorized"}); return
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        if not 0 < length <= 2048: raise ValueError("invalid body")
                        data = json.loads(self.rfile.read(length))
                        if set(data) != {"public_url"}: raise ValueError("invalid fields")
                        if owner.management is None:
                            self._reply(409, {"error": "management_unavailable"}); return
                        self._reply(200, owner.management.set_connection_settings(data["public_url"]))
                    except (ValueError, TypeError):
                        self._reply(400, {"error": "invalid_public_url"})
                    except Exception:
                        self._reply(503, {"error": "connection_update_failed"})
                    return
                if self.path == "/v1/device/apps":
                    if not self._authorized():self._reply(401,{"error":"unauthorized"});return
                    try:
                        length=int(self.headers.get('Content-Length',0))
                        if not 0<length<=64000:raise ValueError('invalid body')
                        data=json.loads(self.rfile.read(length))
                        if set(data)!={'apps'} or not isinstance(data['apps'],list) or any(not isinstance(x,str) for x in data['apps']):raise ValueError('invalid apps')
                        if owner.management is None:self._reply(409,{'error':'management_unavailable'});return
                        self._reply(200,owner.management.set_allowed_apps(data['apps']))
                    except (ValueError,TypeError):self._reply(400,{'error':'invalid_application_list'})
                    except Exception:self._reply(503,{'error':'application_update_failed'})
                    return
                if self.path != "/v1/device/execute-plan":
                    self._reply(404, {"error": "not_found"}); return
                if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {owner.token}"):
                    self._reply(401, {"error": "unauthorized"}); return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 640_000: raise ValueError("invalid body size")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict) or set(data) != {"device_id", "commands", "message_id", "sender_id", "message_type", "group_id"}:
                        raise ValueError("invalid request shape")
                    if data["device_id"] != owner.host.config.device_id: raise ValueError("unknown device")
                    sender_id = data["sender_id"]
                    if not isinstance(sender_id, str) or not 1 <= len(sender_id) <= 64:
                        raise ValueError("invalid sender id")
                    group_id = data["group_id"]
                    message_type = data["message_type"]
                    if not isinstance(message_type, str):
                        raise ValueError("invalid message type")
                    if group_id not in (None, "") and (not isinstance(group_id, str) or len(group_id) > 80):
                        raise ValueError("invalid group id")
                    is_owner = hmac.compare_digest(sender_id, str(owner.host.config.owner_user_id))
                    is_private_owner = is_owner and message_type == "private" and group_id in (None, "")
                    is_group = message_type == "group" and isinstance(group_id, str) and bool(group_id)
                    if not is_private_owner and not is_group:
                        if message_type == "private" and not is_owner:
                            raise ValueError("owner identity required")
                        raise ValueError("owner private or group message required")
                    if not isinstance(data["message_id"], str) or not 1 <= len(data["message_id"]) <= 120:
                        raise ValueError("invalid message id")
                    if not isinstance(data["commands"], list): raise ValueError("commands must be a list")
                    if is_group:
                        for item in data["commands"]:
                            if not isinstance(item, dict) or item.get("command") not in GROUP_COMMANDS:
                                raise ValueError("command not allowed in group")
                    validate_device_plan(data["commands"], owner.host.plugin.policy)
                    result = owner.host.plugin.execute_plan(
                        data["message_id"], data["commands"],
                        device_task_guard=lambda: not owner.stopping.is_set(),
                    )
                    self._reply(200, {"result": result})
                except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, PlanValidationError) as exc:
                    self._reply(400, {"error": str(exc)[:300]})
                except Exception as exc:
                    self._reply(503, {"error": f"host_error:{exc.__class__.__name__}"})
        return Handler

    @property
    def address(self): return self.server.server_address
    def start(self): self.thread.start()
    def stop(self):
        self.stopping.set()
        self.server.shutdown(); self.server.server_close()
        if self.thread.is_alive(): self.thread.join(timeout=2)
