"""Standard-library client for NapCat WebUI."""
from __future__ import annotations
import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:6099"

class NapCatWebUIError(RuntimeError):
    """Sanitized WebUI error; server response details are never echoed."""
class NapCatTransportError(NapCatWebUIError): pass
class NapCatProtocolError(NapCatWebUIError): pass
class NapCatTokenError(NapCatWebUIError): pass
class NapCatTwoFactorRequired(NapCatWebUIError):
    def __init__(self): super().__init__("NapCat WebUI 需要两步验证，请输入验证器中的动态码。")

class NapCatWebUIClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, opener: Any = None, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.timeout = timeout
        self._credential: str | None = None

    @staticmethod
    def _password_hash(token: str) -> str:
        return hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()

    def _request(self, endpoint: str, payload: dict[str, Any] | None = None, *, authenticated: bool = True):
        headers = {"Content-Type": "application/json"}
        if authenticated and self._credential:
            headers["Authorization"] = "Bearer " + self._credential
        req = urllib.request.Request(self.base_url + "/api" + endpoint,
            data=json.dumps(payload or {}).encode("utf-8"), headers=headers, method="POST")
        try:
            response = self.opener.open(req, timeout=self.timeout)
            raw = response.read()
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403): raise NapCatTokenError("NapCat WebUI 凭证无效或已过期，请重新认证。") from None
            raise NapCatTransportError(f"NapCat WebUI 请求失败（HTTP {exc.code}）。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise NapCatTransportError("无法连接 NapCat WebUI，请检查服务地址和运行状态。") from None
        try: result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError): raise NapCatProtocolError("NapCat WebUI 返回了无法识别的数据。") from None
        if not isinstance(result, dict) or "code" not in result:
            raise NapCatProtocolError("NapCat WebUI 返回结构不符合预期。")
        if result.get("code") != 0:
            message = str(result.get("message", ""))
            if any(message.startswith(prefix) for prefix in ("token is invalid", "Unauthorized", "Authorization Failed", "Token has been revoked")):
                raise NapCatTokenError("NapCat WebUI token 无效或凭证已过期。")
            raise NapCatWebUIError("NapCat WebUI 请求未成功，请检查服务状态和登录状态。")
        return result.get("data")

    def authenticate(self, token: str, totp_code: str | None = None) -> dict[str, Any]:
        if not isinstance(token, str) or not token: raise NapCatTokenError("请输入 NapCat WebUI token。")
        payload = {"hash": self._password_hash(token)}
        if totp_code: payload["totpCode"] = totp_code
        data = self._request("/auth/login", payload, authenticated=False)
        if not isinstance(data, dict): raise NapCatProtocolError("NapCat WebUI 登录响应结构不符合预期。")
        if data.get("require2FA") is True: raise NapCatTwoFactorRequired()
        credential = data.get("Credential")
        if not isinstance(credential, str) or not credential:
            raise NapCatTokenError("NapCat WebUI 未签发凭证，请检查 token 或两步验证码。")
        self._credential = credential
        return {"authenticated": True, "require2FA": False}

    def login_status(self) -> dict[str, Any]:
        data = self._request("/QQLogin/CheckLoginStatus")
        if not isinstance(data, dict): raise NapCatProtocolError("NapCat 登录状态响应结构不符合预期。")
        info = self._request("/QQLogin/GetQQLoginInfo") if data.get("isLogin") else {}
        if not isinstance(info, dict): info = {}
        return {"logged_in": bool(data.get("isLogin")), "online": data.get("coreReady") is True,
            "offline": bool(data.get("isOffline")), "login_phase": data.get("loginPhase"),
            "qr_login_accepted": bool(data.get("qrLoginAccepted")), "core_ready": bool(data.get("coreReady")),
            "qr_url": data.get("qrcodeurl") or "", "login_error": data.get("loginError"),
            "account": info.get("uin") or info.get("account") or info.get("nickname")}

    def refresh_qrcode(self) -> dict[str, Any]:
        data = self._request("/QQLogin/RefreshQRcode")
        if not isinstance(data, dict): raise NapCatProtocolError("NapCat 二维码刷新响应结构不符合预期。")
        return {"qr_url": data.get("qrcodeurl") or "", "restarting": bool(data.get("restarting"))}
    def quick_login_list(self) -> list[str]:
        data = self._request("/GetQuickLoginList")
        if not isinstance(data, list):
            raise NapCatProtocolError("NapCat 快速登录列表响应结构不符合预期。")
        return [str(item) for item in data if isinstance(item, (str, int))]

    def set_quick_login(self, uin: str) -> dict[str, Any]:
        if not str(uin).strip():
            raise ValueError("QQ 账号不能为空。")
        self._request("/SetQuickLogin", {"uin": str(uin).strip()})
        return {"uin": str(uin).strip()}

    def auto_login_account(self) -> str | None:
        data = self._request("/GetQuickLoginQQ")
        if data is None or data == "":
            return None
        if isinstance(data, (str, int)):
            return str(data)
        if isinstance(data, dict):
            value = data.get("uin") or data.get("account")
            return str(value) if value else None
        raise NapCatProtocolError("NapCat 自动登录账号响应结构不符合预期。")

    def set_auto_login_account(self, uin: str | None) -> None:
        self._request("/SetQuickLoginQQ", {"uin": str(uin or "")})
    def ensure_websocket(self, port: int = 3001, onebot_token: str = "") -> dict[str, Any]:
        port = int(port)
        if not 1 <= port <= 65535: raise ValueError("WebSocket 端口必须在 1 到 65535 之间。")
        config = self._request("/OB11Config/GetConfig")
        if not isinstance(config, dict): raise NapCatProtocolError("OneBot 配置响应结构不符合预期。")
        network = config.get("network")
        if not isinstance(network, dict): raise NapCatProtocolError("OneBot 配置缺少 network 字段。")
        servers = network.setdefault("websocketServers", [])
        if not isinstance(servers, list): raise NapCatProtocolError("OneBot websocketServers 配置结构不符合预期。")
        target = next((entry for entry in servers if isinstance(entry, dict) and entry.get("name") == "Whale Desktop"), None)
        if target is None:
            occupied = {entry.get("port") for entry in servers if isinstance(entry, dict) and isinstance(entry.get("port"), int)}
            actual_port = port
            while actual_port in occupied:
                actual_port += 1
                if actual_port > 65535:
                    raise NapCatWebUIError("没有可用的消息连接端口，请在高级设置中调整端口。")
            target = {"name": "Whale Desktop", "enable": True, "host": "127.0.0.1", "port": actual_port,
                "messagePostFormat": "array", "reportSelfMessage": False, "token": onebot_token,
                "enableForcePushEvent": True, "debug": False, "heartInterval": 30000}
            servers.append(target)
        else:
            occupied = {entry.get("port") for entry in servers if isinstance(entry, dict) and entry is not target and isinstance(entry.get("port"), int)}
            actual_port = target.get("port") if isinstance(target.get("port"), int) else port
            while actual_port in occupied:
                actual_port += 1
                if actual_port > 65535:
                    raise NapCatWebUIError("没有可用的消息连接端口，请在高级设置中调整端口。")
            target["host"] = "127.0.0.1"
            target["port"] = actual_port
            target["enable"] = True
            target["token"] = onebot_token
            target["messagePostFormat"] = "array"
            target["reportSelfMessage"] = False
        self._request("/OB11Config/SetConfig", {"config": json.dumps(config)})
        return {"enabled": True, "host": "127.0.0.1", "port": actual_port}

__all__ = ["NapCatProtocolError", "NapCatTokenError", "NapCatTransportError", "NapCatTwoFactorRequired", "NapCatWebUIClient", "NapCatWebUIError"]





