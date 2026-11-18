import asyncio
import json

from astrbot_whale_device.linux_qq_service import LinuxQQService, login_endpoint


def test_runtime_selection_keeps_windows_default_and_selects_linux_explicitly(tmp_path):
    assert login_endpoint(tmp_path)[0] == "http://127.0.0.1:6099"
    marker = tmp_path / "qq-linux-runtime.json"
    marker.write_text('{"enabled":true}')
    endpoint, token_path = login_endpoint(tmp_path)
    assert endpoint == "http://127.0.0.1:6109"
    assert token_path.name == "qq-linux-webui.token"
    marker.write_text('{"enabled":false}')
    assert login_endpoint(tmp_path)[0] == "http://127.0.0.1:6099"


def test_existing_controller_uses_selected_service_credentials(tmp_path, monkeypatch):
    from astrbot_whale_device.qq_account import QQAccountController

    (tmp_path / "qq-linux-runtime.json").write_text('{"enabled":true}')
    (tmp_path / "qq-linux-webui.token").write_text("linux-secret")
    seen = []

    class Client:
        def __init__(self, url, **kwargs):
            seen.append(url)

        def authenticate(self, token):
            seen.append(token)

        def login_status(self):
            return {"logged_in": False, "qr_url": "https://example.test/qr"}

    monkeypatch.setattr("astrbot_whale_device.napcat_webui.NapCatWebUIClient", Client)
    qr = QQAccountController(tmp_path / "host.json").login_qr()
    assert qr["available"] is True
    assert seen == ["http://127.0.0.1:6109", "linux-secret"]


def test_onebot_relay_transfers_event_action_and_receipt_without_mutation(
    tmp_path, monkeypatch
):
    import websockets

    data = tmp_path / "dist-exe"
    data.mkdir()
    (data / "qq-linux-onebot.token").write_text("linux-token")
    config = tmp_path / "runtime/astrbot/data"
    config.mkdir(parents=True)
    (config / "cmd_config.json").write_text(
        json.dumps(
            {"platform": [{"id": "whale_qq", "ws_reverse_token": "astrbot-token"}]}
        )
    )
    service = LinuxQQService(tmp_path)
    service._account = "123"
    received = []
    event = json.dumps({"post_type": "message", "self_id": 123, "message": "hello"})
    action = json.dumps({"action": "get_login_info", "echo": "request-1"})
    receipt = json.dumps(
        {"status": "ok", "echo": "request-1", "data": {"user_id": 123}}
    )

    async def run():
        async def linux(ws):
            assert ws.request.headers["Authorization"] == "Bearer linux-token"
            await ws.send(event)
            received.append(await ws.recv())
            await ws.send(receipt)
            await ws.wait_closed()

        async def astrbot(ws):
            assert ws.request.headers["Authorization"] == "Bearer astrbot-token"
            assert ws.request.headers["X-Self-ID"] == "123"
            received.append(await ws.recv())
            await ws.send(action)
            received.append(await ws.recv())
            service._stop.set()

        connect = websockets.connect
        async with (
            websockets.serve(linux, "127.0.0.1", 0) as a,
            websockets.serve(astrbot, "127.0.0.1", 0) as b,
        ):
            ports = {
                "6113": a.sockets[0].getsockname()[1],
                "6199": b.sockets[0].getsockname()[1],
            }

            def local_connect(url, **kwargs):
                port = "6113" if ":6113" in url else "6199"
                return connect(
                    url.replace(":" + port, ":" + str(ports[port])), **kwargs
                )

            monkeypatch.setattr(websockets, "connect", local_connect)
            await asyncio.wait_for(service._forward(), 5)

    asyncio.run(run())
    assert received == [event, action, receipt]
