from astrbot_whale_device.qq_account import QQAccountController

def test_account_controller_persists_logout_without_disabling_auto_login(tmp_path):
    path = tmp_path / "host.json"
    stopped = []
    c = QQAccountController(path, start_login=lambda: None, stop_login=lambda: stopped.append(True))
    c.set_auto_login(True)
    c.login()
    state = c.cancel_login()
    assert state["auto_login"] is True
    assert state["logout_requested"] is True
    assert stopped == [True]
    assert QQAccountController(path).status()["logout_requested"] is True

def test_account_controller_runtime_ack_does_not_expose_qr(tmp_path):
    c = QQAccountController(tmp_path / "host.json")
    state = c.update_runtime(logged_in=True, account="123", qr_url="private")
    assert state["logged_in"] and state["account"] == "123"
    assert "qr_url" not in state
def test_login_qr_reauthenticates_after_stale_client(tmp_path, monkeypatch):
    from types import SimpleNamespace
    c = QQAccountController(tmp_path / "host.json")
    c.path.parent.joinpath("qq-data/NapCat/config").mkdir(parents=True)
    c.path.parent.joinpath("qq-data/NapCat/config/webui.json").write_text('{"token":"x"}')
    c.path.parent.joinpath("host.json").write_text('{}')
    clients = []
    class Client:
        def __init__(self, *a, **k): self.calls = 0; clients.append(self)
        def authenticate(self, token): pass
        def login_status(self):
            self.calls += 1
            if self.calls == 1: raise RuntimeError("stale")
            return {"logged_in": False, "qr_url": ""}
    monkeypatch.setattr("astrbot_whale_device.napcat_webui.NapCatWebUIClient", Client)
    assert c.login_qr()["available"] is False
    assert c._webui_client is None
    c._auth_retry_at = 0
    assert c.login_qr()["available"] is False
    assert len(clients) == 2

def test_expired_heartbeat_never_leaves_login_in_progress(tmp_path):
    import json
    import time
    c = QQAccountController(tmp_path / "host.json")
    c.runtime_path.write_text(json.dumps({"updated_at": time.time()-60, "broker_running": True,
        "online": True, "logged_in": True, "login_in_progress": True}))
    status = c.status()
    assert status["broker_running"] is False
    assert status["login_in_progress"] is False
    assert status["online"] is False
    assert status["login_required"] is True
    c.cancel_login()
    assert c.status()["login_required"] is False
    c.login()
    assert c.status()["login_required"] is True
    assert "重新打开" not in c.status()["error"]


def test_login_qr_refreshes_aged_code_but_does_not_regenerate_every_poll(tmp_path):
    from types import SimpleNamespace

    controller = QQAccountController(tmp_path / "host.json")
    refreshes = []
    controller._webui_client = SimpleNamespace(
        login_status=lambda: {"logged_in": False, "qr_url": "https://example.test/current-code"},
        refresh_qrcode=lambda: refreshes.append(True),
    )
    assert controller.login_qr()["available"]
    assert controller.login_qr()["available"]
    assert len(refreshes) == 1
    controller._qr_refreshed_at -= 91
    assert controller.login_qr()["available"]
    assert len(refreshes) == 2
