import json
from types import SimpleNamespace

import pytest

from astrbot_whale_device.device_management import DeviceManagement
from astrbot_whale_device.host_config import HostConfig
from astrbot_whale_device.models import DeviceEnvelope, DeviceResponse
from astrbot_whale_device.protocol import ProtocolError, decode_envelope
from astrbot_whale_device.validation import PlanValidationError, validate_device_plan


def management(tmp_path, succeeds=True):
    raw = {
        "owner_user_id": "12345",
        "device_id": "phone-one",
        "signing_secret_env": "TEST_SECRET",
        "allowed_packages": ["com.huawei.photos"],
        "blocked_packages": [],
        "model": {
            "provider_id": "openai",
            "model_id": "test",
            "base_url": "https://api.example.com",
        },
    }
    path = tmp_path / "host.json"
    path.write_text(json.dumps(raw))
    rows = [
        SimpleNamespace(
            as_dict=lambda: {
                "state": "ONLINE",
                "device_id": "phone-one",
                "connected_at_ms": 1,
            }
        )
    ]
    calls = []

    def request(command, payload, *, device_id):
        calls.append((command, payload, device_id))
        return DeviceResponse(
            "request",
            device_id,
            "ok" if succeeds else "denied",
            evidence={
                "policy_synced": "true",
                "allowed_packages": json.dumps(payload["allowed_packages"]),
            },
        )

    host = SimpleNamespace(
        config=HostConfig.from_mapping(raw),
        device_server=SimpleNamespace(is_running=True, connections=lambda: rows),
        request_device=request,
        plugin=SimpleNamespace(router=SimpleNamespace()),
    )
    return DeviceManagement(host, path), calls, rows


def test_saved_app_permissions_require_phone_receipt_and_refresh_on_reconnect(tmp_path):
    manager, calls, rows = management(tmp_path)
    assert manager.status()["policy_synced"]
    assert manager.status()["policy_synced"] and len(calls) == 1
    result = manager.set_allowed_apps(["com.huawei.photos", "com.tencent.mm"])
    assert result["synced"] and len(calls) == 2
    assert calls[-1][1]["allowed_packages"] == ["com.huawei.photos", "com.tencent.mm"]
    assert (
        json.loads(manager.config_path.read_text())["allowed_packages"]
        == result["apps"]
    )
    rows.clear()
    assert not manager.status()["policy_synced"]
    rows.append(
        SimpleNamespace(
            as_dict=lambda: {
                "state": "ONLINE",
                "device_id": "phone-one",
                "connected_at_ms": 2,
            }
        )
    )
    assert manager.status()["policy_synced"] and len(calls) == 3


def test_phone_rejection_is_not_reported_as_successful_save(tmp_path):
    manager, calls, rows = management(tmp_path, succeeds=False)
    result = manager.set_allowed_apps(["com.huawei.photos"])
    assert not result["synced"] and "尚未确认" in result["message"]


def test_management_command_uses_protocol_but_is_unavailable_to_model():
    payload = {"allowed_packages": ["com.huawei.photos"], "blocked_packages": []}
    envelope = DeviceEnvelope(
        protocol_version=1,
        request_id="req",
        device_id="phone-one",
        command="host.policy.sync",
        deadline_ms=8000,
        idempotency_key="desktop:req",
        payload=payload,
        signature="signed",
    )
    assert decode_envelope(json.dumps(envelope.as_dict())).payload == payload
    with pytest.raises(PlanValidationError):
        validate_device_plan([{"command": "host.policy.sync", "payload": payload}])
    payload["allowed_packages"] = ["invalid package"]
    with pytest.raises(ProtocolError):
        decode_envelope(json.dumps(envelope.as_dict()))
