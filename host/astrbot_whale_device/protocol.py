from __future__ import annotations

import json
import re
from typing import Any

from .models import DeviceEnvelope, DeviceResponse


ALLOWED_COMMANDS = {
    "device.app.list",
    "device.app.launch",
    "device.ui.observe",
    "device.ui.tap",
    "device.ui.long_press",
    "device.ui.scroll",
    "device.ui.input",
    "device.ui.back",
    "device.ui.home",
    "device.ui.recents",
    "device.ui.notifications",
    "device.system.settings",
    "device.camera.capture",
    "device.media.pick",
    "device.media.upload",
    "device.media.share",
    "device.gallery.list",
    "device.gallery.read",
    "device.screen.capture",
    "device.telephony.dial",
    "device.contacts.search",
    "device.sms.compose",
    "device.sms.send",
    "device.device_status",
    "device.system.unlock",
}


class ProtocolError(ValueError):
    pass


def encode_envelope(envelope: DeviceEnvelope) -> str:
    if not envelope.signature:
        raise ProtocolError("cannot encode unsigned envelope")
    return json.dumps(envelope.as_dict(), ensure_ascii=False, separators=(",", ":"))


def decode_envelope(raw: str | bytes) -> DeviceEnvelope:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON envelope") from exc
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be an object")
    required = (
        "protocol_version",
        "request_id",
        "device_id",
        "command",
        "deadline_ms",
        "idempotency_key",
        "counter",
        "issued_at_ms",
        "payload",
        "signature",
    )
    if any(key not in data for key in required):
        raise ProtocolError("missing envelope field")
    if set(data) - set(required):
        raise ProtocolError("unknown envelope field")
    if (
        isinstance(data["protocol_version"], bool)
        or not isinstance(data["protocol_version"], int)
        or data["protocol_version"] != 1
    ):
        raise ProtocolError("unsupported protocol version")
    if (
        data["command"] not in ALLOWED_COMMANDS
        and data["command"] != "host.policy.sync"
    ):
        raise ProtocolError("unknown command")
    if (
        not isinstance(data["request_id"], str)
        or not data["request_id"]
        or len(data["request_id"]) > 80
    ):
        raise ProtocolError("invalid request_id")
    if (
        not isinstance(data["device_id"], str)
        or not 3 <= len(data["device_id"]) <= 80
        or re.fullmatch(r"[A-Za-z0-9._:-]+", data["device_id"]) is None
    ):
        raise ProtocolError("invalid device_id")
    if (
        not isinstance(data["idempotency_key"], str)
        or not 1 <= len(data["idempotency_key"]) <= 160
    ):
        raise ProtocolError("invalid idempotency_key")
    if (
        isinstance(data["deadline_ms"], bool)
        or not isinstance(data["deadline_ms"], int)
        or not 100 <= data["deadline_ms"] <= 300_000
    ):
        raise ProtocolError("invalid deadline_ms")
    if (
        isinstance(data.get("counter", 0), bool)
        or not isinstance(data.get("counter", 0), int)
        or data.get("counter", 0) < 0
    ):
        raise ProtocolError("invalid counter")
    if (
        isinstance(data.get("issued_at_ms", 0), bool)
        or not isinstance(data.get("issued_at_ms", 0), int)
        or data.get("issued_at_ms", 0) < 0
    ):
        raise ProtocolError("invalid issued_at_ms")
    if not isinstance(data["signature"], str) or not data["signature"]:
        raise ProtocolError("missing signature")
    if not isinstance(data["payload"], dict):
        raise ProtocolError("payload must be an object")
    if data["command"] == "host.policy.sync":
        policy = data["payload"]
        if set(policy) != {"allowed_packages", "blocked_packages"}:
            raise ProtocolError("invalid host policy fields")
        for packages in policy.values():
            if (
                not isinstance(packages, list)
                or len(packages) > 500
                or any(
                    not isinstance(p, str)
                    or not 3 <= len(p) <= 255
                    or re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", p) is None
                    for p in packages
                )
            ):
                raise ProtocolError("invalid host policy packages")
        if set(policy["allowed_packages"]) & set(policy["blocked_packages"]):
            raise ProtocolError("host policy packages overlap")
    return DeviceEnvelope(
        protocol_version=int(data["protocol_version"]),
        request_id=str(data["request_id"]),
        device_id=str(data["device_id"]),
        command=str(data["command"]),
        deadline_ms=int(data["deadline_ms"]),
        idempotency_key=str(data["idempotency_key"]),
        payload=dict(data["payload"]),
        counter=int(data.get("counter", 0)),
        issued_at_ms=int(data.get("issued_at_ms", 0)),
        signature=str(data.get("signature", "")),
    )


def encode_response(response: DeviceResponse) -> str:
    return json.dumps(response.as_dict(), ensure_ascii=False, separators=(",", ":"))
