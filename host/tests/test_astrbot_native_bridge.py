import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import pytest
from astrbot_whale_device.astrbot_native_bridge import AstrBotDeviceRPC

TOKEN = "t" * 48

def build_host():
    calls = []
    plugin = SimpleNamespace(
        policy=object(),
        execute_plan=lambda message_id, commands, **kwargs: calls.append((message_id, commands, kwargs)) or {"kind": "device_task", "results": []},
    )
    host = SimpleNamespace(config=SimpleNamespace(device_id="phone-one", owner_user_id="12345"), plugin=plugin)
    return host, calls

def post(rpc, token=TOKEN, **overrides):
    data = {"device_id": "phone-one", "message_id": "msg-1", "commands": [{"command": "device.device_status", "payload": {}}], "sender_id": "12345", "message_type": "private", "group_id": ""}
    data.update(overrides)
    request = Request(f"http://127.0.0.1:{rpc.address[1]}/v1/device/execute-plan", data=json.dumps(data).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())

@pytest.fixture
def running_rpc():
    host, calls = build_host()
    rpc = AstrBotDeviceRPC(host, TOKEN, port=0)
    rpc.start()
    try:
        yield rpc, calls
    finally:
        rpc.stop()

def test_rpc_requires_bearer_and_trusted_owner_private_context(running_rpc):
    rpc, calls = running_rpc
    assert post(rpc, token="wrong") == (401, {"error": "unauthorized"})
    assert post(rpc, sender_id="999") == (400, {"error": "owner identity required"})
    assert post(rpc, message_type="group", group_id="g-1", commands=[{"command": "device.media.share", "payload": {"asset_id": "asset-1"}}])[0] == 400
    assert calls == []

def test_rpc_accepts_safe_group_requests_for_non_owner(running_rpc):
    rpc, calls = running_rpc
    status, result = post(
        rpc,
        sender_id="67890",
        message_type="group",
        group_id="g-1",
        commands=[{"command": "device.gallery.list", "payload": {}}],
    )
    assert status == 200
    assert result["result"]["kind"] == "device_task"
    assert len(calls) == 1

def test_rpc_accepts_group_gallery_read(running_rpc):
    rpc, calls = running_rpc
    command = {"command": "device.gallery.read", "payload": {"media_id": "photo-1"}}
    status, _ = post(
        rpc,
        sender_id="67890",
        message_type="group",
        group_id="g-1",
        commands=[command],
    )
    assert status == 200
    assert calls[0][1] == [command]

@pytest.mark.parametrize("command", [
    "device.sms.send",
    "device.contacts.search",
    "device.telephony.dial",
    "device.media.share",
    "device.media.upload",
    "device.system.settings",
    "host.policy.sync",
    "device.camera.capture",
    "device.ui.notifications",
    "device.ui.tap",
    "device.ui.input",
])
def test_rpc_rejects_high_risk_group_commands(running_rpc, command):
    rpc, calls = running_rpc
    status, result = post(
        rpc,
        sender_id="67890",
        message_type="group",
        group_id="g-1",
        commands=[{"command": command, "payload": {}}],
    )
    assert status == 400
    assert result["error"] == "command not allowed in group"
    assert calls == []

def test_rpc_accepts_only_validated_fixed_device_plan(running_rpc):
    rpc, calls = running_rpc
    status, result = post(rpc)
    assert status == 200
    assert result["result"]["kind"] == "device_task"
    assert len(calls) == 1
    assert calls[0][0:2] == ("msg-1", [{"command": "device.device_status", "payload": {}}])
    assert calls[0][2]["device_task_guard"]()
    assert post(rpc, commands=[{"command": "shell.exec", "payload": {}}])[0] == 400
