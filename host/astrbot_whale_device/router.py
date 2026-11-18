from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .models import (
    DEVICE_DEADLINE_MAX_MS,
    DEVICE_DEADLINE_MIN_MS,
    MODEL_TIMEOUT_MS,
    DecisionMode,
    DevicePlan,
    IncomingMessage,
    ModelDecision,
    Principal,
)
from .security import PackagePolicy, PrincipalGate
from .validation import PlanValidationError, validate_device_plan


class DecisionError(ValueError):
    """模型输出不符合三选一固定协议。任何失败都直接抛出，不做修复。"""


class ModelTimeoutError(TimeoutError):
    """模型请求超过固定超时预算。"""


MAX_DECISION_TEXT_CHARS = 20_000
MAX_DECISION_MEDIA_ITEMS = 8
DECISION_KEYS = {"mode", "text", "media", "device_plan"}
COMMAND_KEYS = {"command", "payload"}
MEDIA_SEGMENT_RULES: dict[str, frozenset[str]] = {
    "face": frozenset({"id"}),
    "image": frozenset({"file"}),
    "record": frozenset({"file"}),
    "video": frozenset({"file"}),
}


def _reject_media(media: list[Any]) -> None:
    if len(media) > MAX_DECISION_MEDIA_ITEMS:
        raise DecisionError("media list is too long")
    for item in media:
        if not isinstance(item, dict):
            raise DecisionError("media item must be an object")
        kind = item.get("type")
        if not isinstance(kind, str) or kind not in MEDIA_SEGMENT_RULES:
            raise DecisionError("unsupported media segment")
        allowed = MEDIA_SEGMENT_RULES[kind]
        if set(item) - (allowed | {"type"}):
            raise DecisionError("unknown media field")
        if not allowed.issubset(item):
            raise DecisionError("media segment is missing a field")
        for key in allowed:
            if not isinstance(item[key], str) or not item[key]:
                raise DecisionError("media field must be a non-empty string")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise DecisionError("duplicate decision field")
        seen.add(key)
    return dict(pairs)


def parse_model_decision(raw: str) -> ModelDecision:
    """把模型输出解析成固定决策。

    拒绝矩阵（任何一条命中都直接抛 DecisionError，绝不修复、绝不猜字段）：
    - 非字符串 / 非 JSON / JSON 不是 object
    - 出现未知顶层字段
    - mode 缺失、不是字符串、不在 REPLY/SILENT/DEVICE_TASK
    - text 不是字符串，或超长
    - media 不是列表、元素形状或类型不在固定集合、超过 8 条
    - REPLY/SILENT 携带了非 null 的 device_plan
    - DEVICE_TASK 的 device_plan 不是对象、出现未知字段、commands 不是 1..64 个对象
    - 命令缺少 command/payload、带未知字段、payload 形状/取值范围不合规
    """
    if not isinstance(raw, str):
        raise DecisionError("model output must be text")
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise DecisionError("model output must be JSON") from exc
    if not isinstance(data, dict):
        raise DecisionError("model output must be an object")
    if set(data) - DECISION_KEYS:
        raise DecisionError("unknown decision field")
    try:
        mode = DecisionMode(data["mode"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionError("invalid decision mode") from exc
    text = data.get("text", "")
    media = data.get("media", [])
    plan_raw = data.get("device_plan")
    if not isinstance(text, str):
        raise DecisionError("invalid decision fields")
    if len(text) > MAX_DECISION_TEXT_CHARS:
        raise DecisionError("decision text is too long")
    if not isinstance(media, list):
        raise DecisionError("invalid decision fields")
    _reject_media(media)
    if mode is DecisionMode.DEVICE_TASK:
        if not isinstance(plan_raw, dict):
            raise DecisionError("device task requires a device plan")
        if set(plan_raw) - {"commands"}:
            raise DecisionError("unknown device plan field")
        commands_raw = plan_raw.get("commands")
        if not isinstance(commands_raw, list):
            raise DecisionError("device task requires commands")
        if not 1 <= len(commands_raw) <= 64:
            raise DecisionError("device task requires 1..64 commands")
        commands: list[dict[str, Any]] = []
        for command in commands_raw:
            if not isinstance(command, dict):
                raise DecisionError("invalid device command")
            if set(command) - COMMAND_KEYS:
                raise DecisionError("unknown device command field")
            name = command.get("command")
            payload = command.get("payload")
            if not isinstance(name, str) or not isinstance(payload, dict):
                raise DecisionError("invalid device command")
            commands.append({"command": name, "payload": dict(payload)})
        try:
            validate_device_plan(commands, None)
        except PlanValidationError as exc:
            raise DecisionError(str(exc)) from exc
        plan = DevicePlan(commands)
    else:
        if plan_raw is not None:
            raise DecisionError("non-task decision cannot contain device plan")
        plan = None
    return ModelDecision(mode=mode, text=text, media=list(media), device_plan=plan)


def _run_with_timeout(
    fn: Callable[[], str], timeout_ms: int,
) -> str:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样回传给调用方决策
            box["error"] = exc

    thread = threading.Thread(target=target, name="whale-model-call", daemon=True)
    thread.start()
    thread.join(max(timeout_ms, 1) / 1000)
    if thread.is_alive():
        raise ModelTimeoutError(f"model request exceeded {timeout_ms} ms")
    if "error" in box:
        raise box["error"]
    return box["value"]


class MessageRouter:
    """PrincipalGate 之后的唯一决策入口。

    公开签名向后兼容：`MessageRouter(owner_user_id, model_decider, package_policy)`
    与 `route(message)` 保持不变；新增能力全部是关键字可选参数。
    """

    def __init__(
        self,
        owner_user_id: str,
        model_decider: Callable[[IncomingMessage, Principal, bool], str],
        package_policy: PackagePolicy | None = None,
        *,
        fallback_model_decider: Callable[[IncomingMessage, Principal, bool], str] | None = None,
        model_timeout_ms: int = MODEL_TIMEOUT_MS,
    ) -> None:
        self.gate = PrincipalGate(owner_user_id)
        self.package_policy = package_policy or PackagePolicy()
        self.model_decider = model_decider
        self.fallback_model_decider = fallback_model_decider
        self.model_timeout_ms = model_timeout_ms

    # -- 模型调用：固定超时 + 至多一次固定备用模型切换 ----------------------
    def _decide(self, message: IncomingMessage, principal: Principal, can_use_device: bool) -> str:
        try:
            return _run_with_timeout(
                lambda: self.model_decider(message, principal, can_use_device),
                self.model_timeout_ms,
            )
        except Exception as primary_error:
            if self.fallback_model_decider is None:
                raise
            try:
                return _run_with_timeout(
                    lambda: self.fallback_model_decider(message, principal, can_use_device),
                    self.model_timeout_ms,
                )
            except Exception as fallback_error:
                raise ModelTimeoutError(
                    f"primary={primary_error.__class__.__name__} "
                    f"fallback={fallback_error.__class__.__name__}"
                ) from fallback_error

    def route(self, message: IncomingMessage) -> dict[str, Any]:
        principal = self.gate.resolve(message)
        can_use_device = self.gate.can_control(message)
        try:
            raw = self._decide(message, principal, can_use_device)
        except ModelTimeoutError as exc:
            return {"kind": "failed", "reason": "model_unavailable", "principal": principal.value, "detail": str(exc)}
        decision = parse_model_decision(raw)
        if decision.mode is DecisionMode.DEVICE_TASK and not can_use_device:
            return {"kind": "silent", "reason": "guest_device_request", "principal": principal.value}
        if decision.mode is DecisionMode.SILENT:
            return {"kind": "silent", "principal": principal.value}
        if decision.mode is DecisionMode.REPLY:
            return {"kind": "reply", "principal": principal.value, "text": decision.text, "media": decision.media}
        try:
            validate_device_plan(decision.device_plan.commands if decision.device_plan else [], self.package_policy)
        except PlanValidationError as exc:
            return {"kind": "failed", "reason": str(exc), "principal": principal.value}
        return {
            "kind": "device_task",
            "principal": principal.value,
            "plan": {"commands": decision.device_plan.commands if decision.device_plan else []},
            "text": decision.text,
        }

    def device_deadline_ms(self, requested_ms: int | None = None) -> int:
        """设备命令超时上限由主机夹紧；模型输出无法放宽，只可能被收紧。"""
        value = DEVICE_DEADLINE_MAX_MS if requested_ms is None else requested_ms
        if isinstance(value, bool) or not isinstance(value, int):
            return DEVICE_DEADLINE_MAX_MS
        return max(DEVICE_DEADLINE_MIN_MS, min(value, DEVICE_DEADLINE_MAX_MS))
