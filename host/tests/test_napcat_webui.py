from __future__ import annotations
import hashlib, json
from urllib.request import Request
import pytest
from astrbot_whale_device.napcat_webui import NapCatTokenError, NapCatTwoFactorRequired, NapCatWebUIClient, NapCatWebUIError
class Response:
    def __init__(self, value): self.value = json.dumps(value).encode()
    def read(self): return self.value
    def close(self): self.closed = True
class Opener:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    def open(self, request: Request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception): raise result
        return Response(result)
def ok(data): return {"code": 0, "message": "success", "data": data}
def test_authenticate_uses_official_hash_and_bearer_credential():
    opener = Opener([ok({"Credential": "credential-base64"}), ok({"isLogin": False})])
    client = NapCatWebUIClient(opener=opener)
    assert client.authenticate("secret") == {"authenticated": True, "require2FA": False}
    assert json.loads(opener.requests[0].data) == {"hash": hashlib.sha256(b"secret.napcat").hexdigest()}
    client.login_status()
    assert opener.requests[1].get_header("Authorization") == "Bearer credential-base64"
    assert "secret" not in repr(client)
def test_two_factor_and_invalid_token_have_structured_errors_without_echo():
    opener = Opener([ok({"require2FA": True}), {"code": 1, "message": "token is invalid leaked-secret"}])
    client = NapCatWebUIClient(opener=opener)
    with pytest.raises(NapCatTwoFactorRequired): client.authenticate("secret")
    with pytest.raises(NapCatTokenError) as error: client.authenticate("secret", "123456")
    assert "leaked-secret" not in str(error.value)
    assert json.loads(opener.requests[1].data)["totpCode"] == "123456"
def test_login_status_normalizes_phase_qr_account_and_online_state():
    opener = Opener([ok({"isLogin": True, "isOffline": False, "loginPhase": "initializing", "qrLoginAccepted": True, "coreReady": True, "qrcodeurl": "https://qr/secret", "loginError": None}), ok({"uin": "12345", "online": True})])
    client = NapCatWebUIClient(opener=opener); client._credential = "cred"
    assert client.login_status() == {"logged_in": True, "online": True, "offline": False, "login_phase": "initializing", "qr_login_accepted": True, "core_ready": True, "qr_url": "https://qr/secret", "login_error": None, "account": "12345"}
def test_ensure_websocket_preserves_other_configuration_and_updates_matching_server():
    existing = {"network": {"websocketServers": [{"host": "127.0.0.1", "port": 3001, "enable": False, "token": "old"}, {"host": "0.0.0.0", "port": 9000, "enable": True, "token": "other"}], "httpServers": [{"port": 3000, "enable": True}]}, "parseMultMsg": True}
    opener = Opener([ok(existing), ok(None)]); client = NapCatWebUIClient(opener=opener); client._credential = "cred"
    assert client.ensure_websocket(onebot_token="ob-token") == {"enabled": True, "host": "127.0.0.1", "port": 3002}
    posted = json.loads(opener.requests[1].data)["config"]
    assert isinstance(posted, str)
    written = json.loads(posted)
    assert written["network"]["websocketServers"][0] == existing["network"]["websocketServers"][0]
    assert written["network"]["websocketServers"][1] == existing["network"]["websocketServers"][1]
    whale = written["network"]["websocketServers"][2]
    assert whale["name"] == "Whale Desktop" and whale["enable"] is True
    assert whale["port"] == 3002 and whale["token"] == "ob-token"
    assert whale["messagePostFormat"] == "array" and whale["reportSelfMessage"] is False
    assert written["network"]["httpServers"] == existing["network"]["httpServers"] and written["parseMultMsg"] is True
def test_ensure_websocket_idempotently_updates_own_entry_and_avoids_port_conflict():
    config = {"network": {"websocketServers": [
        {"name": "existing", "host": "0.0.0.0", "port": 3001, "token": "keep"},
        {"name": "Whale Desktop", "host": "0.0.0.0", "port": 3001, "token": "stale", "messagePostFormat": "string", "reportSelfMessage": True}],
        "httpServers": []}}
    opener = Opener([ok(config), ok(None), ok(config), ok(None)])
    client = NapCatWebUIClient(opener=opener); client._credential = "cred"
    assert client.ensure_websocket(3001, "first")["port"] == 3002
    assert client.ensure_websocket(3001, "second")["port"] == 3002
    updated = json.loads(json.loads(opener.requests[3].data)["config"])["network"]["websocketServers"]
    assert len(updated) == 2
    assert updated[0]["token"] == "keep" and updated[0]["port"] == 3001
    assert updated[1]["token"] == "second" and updated[1]["port"] == 3002
    assert updated[1]["messagePostFormat"] == "array" and updated[1]["reportSelfMessage"] is False
def test_webui_errors_do_not_include_response_secrets():
    client = NapCatWebUIClient(opener=Opener([{"code": 1, "message": "QR secret-token-value"}])); client._credential = "credential-secret"
    with pytest.raises(NapCatWebUIError) as error: client.login_status()
    assert "secret-token-value" not in str(error.value) and "credential-secret" not in str(error.value)





