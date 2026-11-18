from __future__ import annotations

from typing import Any

from .protocol import ALLOWED_COMMANDS
from .security import PackagePolicy


class PlanValidationError(ValueError):
    pass


# `ui_target`：四个 UI 动作命令共用的目标指纹字段，镜像
# `device/.../CommandSpec.kt` 的 `uiTargetFields()` 与
# `contracts/device-commands.schema.json` 的 `ui_target`。
_UI_TARGET_REQUIRED = {"snapshot_id", "target_id", "selector_hash", "expected_package"}
_UI_TARGET_ALLOWED = _UI_TARGET_REQUIRED | {"expected_state"}

# 每条命令的 (required, allowed) payload 形状表，镜像 M06 固定能力清单。
SHAPES: dict[str, tuple[set[str], set[str]]] = {
    "device.app.list": (set(), set()),
    "device.screen.capture": (set(), set()),
    "device.app.launch": ({"package_name"}, {"package_name"}),
    "device.ui.observe": (set(), {"mode", "package_name"}),
    "device.ui.tap": (_UI_TARGET_REQUIRED, _UI_TARGET_ALLOWED),
    "device.ui.long_press": (_UI_TARGET_REQUIRED | {"duration_ms"}, _UI_TARGET_ALLOWED | {"duration_ms"}),
    "device.ui.scroll": (_UI_TARGET_REQUIRED | {"direction"}, _UI_TARGET_ALLOWED | {"direction", "amount"}),
    "device.ui.input": (_UI_TARGET_REQUIRED | {"text"}, _UI_TARGET_ALLOWED | {"text"}),
    "device.ui.back": (set(), set()),
    "device.ui.home": (set(), set()),
    "device.ui.recents": (set(), set()),
    "device.ui.notifications": (set(), set()),
    "device.system.settings": (set(), {"action"}),
    "device.camera.capture": (set(), {"lens", "quality"}),
    "device.media.pick": ({"media_type", "max_items"}, {"media_type", "max_items"}),
    "device.media.upload": ({"asset_id"}, {"asset_id"}),
    "device.media.share": ({"asset_id"}, {"asset_id", "target"}),
    "device.gallery.list": (set(), {"limit", "offset"}),
    "device.gallery.read": ({"media_id"}, {"media_id"}),
    "device.telephony.dial": ({"number"}, {"number"}),
    "device.contacts.search": ({"query"}, {"query"}),
    "device.sms.compose": ({"number", "text"}, {"number", "text"}),
    "device.sms.send": ({"number", "text"}, {"number", "text"}),
    "device.device_status": (set(), set()),
    "device.system.unlock": (set(), set()),
}

# 字符串字段的 (minLength, maxLength, allowedValues)；minLength 为 0 表示 schema 未声明下限。
_STRING_RULES: dict[str, tuple[int, int | None, frozenset[str] | None]] = {
    "package_name": (3, 255, None),
    "snapshot_id": (1, None, None),
    "target_id": (1, None, None),
    "selector_hash": (1, None, None),
    "expected_package": (1, 255, None),
    "mode": (0, None, frozenset({"auto", "tree", "vision"})),
    "lens": (0, None, frozenset({"back", "front"})),
    "quality": (0, None, frozenset({"standard", "high"})),
    "media_type": (0, None, frozenset({"image", "video", "audio", "any"})),
    "asset_id": (1, 160, None),
    "media_id": (1, 80, None),
    "target": (0, 160, None),
    "number": (3, 32, None),
    "query": (1, 120, None),
    "direction": (0, None, frozenset({"up", "down", "left", "right"})),
    "action": (0, 200, None),
}

# `text` 字段在三条命令里长度不同，必须按命令区分。
_TEXT_RULES: dict[str, tuple[int, int | None]] = {
    "device.ui.input": (0, 20_000),
    "device.sms.compose": (1, 5_000),
    "device.sms.send": (1, 5_000),
}

# 整数字段的 (minValue, maxValue)。
_INT_RULES: dict[str, tuple[int, int]] = {
    "duration_ms": (300, 30_000),
    "amount": (1, 2_000),
    "max_items": (1, 20),
    "limit": (1, 20),
    "offset": (0, 1000),
}


def _check_string(command: str, key: str, value: str) -> None:
    if key == "text":
        low, high = _TEXT_RULES[command]
        if len(value) < low:
            raise PlanValidationError(f"{key} is shorter than the declared minimum")
        if high is not None and len(value) > high:
            raise PlanValidationError(f"{key} is longer than the declared maximum")
        return
    rule = _STRING_RULES.get(key)
    if rule is None:
        return
    low, high, allowed = rule
    if len(value) < low:
        raise PlanValidationError(f"{key} is shorter than the declared minimum")
    if high is not None and len(value) > high:
        raise PlanValidationError(f"{key} is longer than the declared maximum")
    if allowed is not None and value not in allowed:
        raise PlanValidationError(f"{key} is not in the declared value set")


def _check_int(key: str, value: int) -> None:
    rule = _INT_RULES.get(key)
    if rule is None:
        return
    low, high = rule
    if not low <= value <= high:
        raise PlanValidationError(f"{key} is out of the declared range")


def _payload_shape(command: str, payload: dict[str, Any]) -> None:
    shape = SHAPES.get(command)
    if shape is None:
        raise PlanValidationError("unknown device command")
    required, allowed = shape
    if not required.issubset(payload):
        raise PlanValidationError(f"missing payload field for {command}")
    if set(payload) - allowed:
        raise PlanValidationError(f"unknown payload field for {command}")
    for key, value in payload.items():
        if key == "expected_state":
            if not isinstance(value, dict):
                raise PlanValidationError("expected_state must be an object")
            continue
        if key in _INT_RULES:
            if not isinstance(value, int) or isinstance(value, bool):
                raise PlanValidationError(f"{key} must be an integer")
            _check_int(key, value)
            continue
        if key in _STRING_RULES or key == "text":
            if not isinstance(value, str):
                raise PlanValidationError(f"{key} must be a string")
            _check_string(command, key, value)


def validate_device_plan(commands: list[dict[str, Any]], policy: PackagePolicy | None = None) -> None:
    """校验 1..64 条固定命令。

    `policy` 为 `None` 时只做结构校验（命令白名单、payload 形状、取值范围），
    跳过 `device.app.launch` 的主机预检白名单。这一分支专供 `parse_model_decision`
    使用：包名白名单属于 PrincipalGate/PackagePolicy 的职责，必须在 `MessageRouter`
    里、且在「访客请求设备任务一律静默」之后才生效。
    """
    if not commands or len(commands) > 64:
        raise PlanValidationError("device plan must contain 1..64 commands")
    for item in commands:
        if not isinstance(item, dict):
            raise PlanValidationError("command must be an object")
        name = item.get("command")
        payload = item.get("payload")
        if name not in ALLOWED_COMMANDS:
            raise PlanValidationError("unknown device command")
        if not isinstance(payload, dict):
            raise PlanValidationError("command payload must be an object")
        _payload_shape(name, payload)
        if name == "device.app.launch" and policy is not None:
            package_name = payload.get("package_name")
            if not isinstance(package_name, str) or not policy.allow(package_name):
                raise PlanValidationError("launch target is not in the host preflight allowlist")
