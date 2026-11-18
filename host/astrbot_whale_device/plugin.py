from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import Any

from .models import DeviceEnvelope, DeviceResponse, IncomingMessage, Principal
from .onebot import OneBotEventError, OneBotSender, parse_event
from .onebot_lifecycle import OneBotLifecycle, OutboundAction, SendOutcome
from .router import MessageRouter
from .security import PackagePolicy, PrincipalGate, sign_hmac
from .storage import TaskStore
from .transport import DeviceTransport
from .validation import PlanValidationError, validate_device_plan
#: 主人上下文才挂载的设备工具名（客人上下文里为空）。
DEVICE_TOOL_NAME = "device_execute_plan"
#: 群聊/客人一律不可用的能力说明，写进上下文供模型遵守。
GUEST_DENIED_TOOLS = ("device_execute_plan", "device_state_query", "device_media_capture", "schedule_task")
#: 群聊 / 客人的固定上下文标记：不含设备工具。
CONTEXT_CHANNEL = "onebot"


@dataclass
class PluginConfig:
    owner_user_id: str
    device_id: str
    allowed_packages: set[str]
    blocked_packages: set[str]
    signing_secret: bytes = b""
    #: 明确允许的本地素材目录；为空表示只接受 AssetStore 资产。
    allowed_media_roots: tuple[str, ...] = ()


class WhaleDevicePlugin:
    """AstrBot integration boundary.

    The concrete AstrBot decorator/API binding belongs here. The core router is
    intentionally framework-free so it can be tested and upgraded independently.

    事件处理顺序（M03 开发文档固定 7 步，逐条对应本类方法）：
    1. `ingest_event` / `on_onebot_event`：丢弃非 message 事件（parse_event 拒绝）。
    2. `on_onebot_event`：丢弃机器人自发消息（self_id == user_id）。
    3. `on_onebot_message`：写入 inbox(message_id, user_id)。
    4. `on_onebot_message`：检查 message_id 的所有者是否一致。
    5. `on_onebot_message`：PrincipalGate 判定主人/客人/群聊。
    6. `build_context`：客人上下文不含设备工具；主人上下文含设备工具。
    7. `on_onebot_message`：保存最终 effect；重复事件只返回已保存 effect。
    """

    def __init__(
        self,
        config: PluginConfig,
        model_decider,
        transport: DeviceTransport,
        store: TaskStore | None = None,
        *,
        sender: OneBotSender | None = None,
        lifecycle: OneBotLifecycle | None = None,
        deliver_replies: bool = True,
        defer_device_tasks: bool = False,
    ) -> None:
        self.config = config
        self.policy = PackagePolicy(config.allowed_packages, config.blocked_packages)
        self.router = MessageRouter(config.owner_user_id, model_decider, self.policy)
        self.gate = PrincipalGate(config.owner_user_id)
        self.transport = transport
        self.store = store or TaskStore()
        self.sender = sender
        # 默认行为不变：插件自己投递回复。接桥接层时置 False，由桥接层负责投递。
        self.deliver_replies = deliver_replies
        # 默认行为不变：插件自己执行设备计划。置 True 时把计划交给桥接层用适配器的
        # to_device_envelopes 执行，避免同一份计划两条路径同时下发。
        self.defer_device_tasks = defer_device_tasks
        self.lifecycle = lifecycle or OneBotLifecycle(self.store, config.device_id, sender)

    # ---- 步骤 1-2：入站闸门 ---------------------------------------------
    def on_onebot_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """OneBot 事件入口：非 message 事件与机器人自发消息在这里被静默丢弃。"""
        try:
            message = parse_event(event)
        except OneBotEventError as exc:
            # 步骤 1：非 message / 字段不合法 → 丢弃，不写 inbox、不触发模型。
            return self.discard(exc.reason)
        # 步骤 2：机器人自发消息 → 丢弃。
        if message.is_bot:
            return self.discard("bot_self_message")
        return self.on_onebot_message(message)

    @staticmethod
    def discard(reason: str) -> dict[str, Any]:
        return {"kind": "silent", "reason": reason}

    # ---- 步骤 3-7：消息处理 ---------------------------------------------
    def on_onebot_message(self, message: IncomingMessage) -> dict[str, Any]:
        effect_key = f"message:{message.message_id}"
        # 步骤 2 的直调兜底：直接传模型对象时同样丢弃自发消息。
        if message.is_bot:
            return self.discard("bot_self_message")
        # 步骤 3：写入 inbox(message_id, user_id)。
        if not self.store.add_message(message.message_id, message.user_id, message.text, int(time.time() * 1000)):
            # 步骤 4：message_id 已被别的 user_id 占用 → 所有者不一致，静默。
            if self.store.message_owner(message.message_id) != message.user_id:
                return self.discard("message_id_owner_mismatch")
            # 步骤 7：重复事件只返回已保存 effect；没有 effect 说明仍在处理中。
            cached = self.store.effect(effect_key)
            return json.loads(cached) if cached else {"kind": "queued", "reason": "duplicate_message_in_flight"}
        # 步骤 5：PrincipalGate 判定主人/客人/群聊（设备控制只看 user_id + 私聊）。
        principal = self.gate.resolve(message)
        can_control = self.gate.can_control(message)
        # 步骤 6：客人上下文不含设备工具，主人上下文含设备工具。
        context = self.build_context(message, principal, can_control)
        try:
            routed = self.router.route(message)
            if routed.get("kind") == "device_task" and not can_control:
                # 纵深防御：路由层已拦，这里再兜一次，绝不因模型输出放行设备任务。
                result: dict[str, Any] = {"kind": "silent", "reason": "guest_device_request", "principal": principal.value}
            elif routed.get("kind") == "device_task":
                if self.defer_device_tasks:
                    # 计划交回调用方（桥接层）用 adapter 执行；这里只做校验与占位，不碰设备。
                    # kind 仍保持 device_task：adapter 的 to_device_envelopes 只认这个值。
                    result = {
                        "kind": "device_task",
                        "principal": principal.value,
                        "deferred": True,
                        "plan": {"commands": list(routed["plan"]["commands"])},
                    }
                else:
                    result = self.execute_plan(message.message_id, routed["plan"]["commands"])
                    result["principal"] = principal.value
            else:
                result = dict(routed)
            result["context"] = context
            if result.get("kind") == "reply" and self.deliver_replies:
                self.deliver_reply(message, result)
        except Exception as exc:
            result = {"kind": "failed", "reason": f"router_error:{exc.__class__.__name__}", "principal": principal.value}
        # 步骤 7：保存最终 effect。
        self.store.save_effect(effect_key, json.dumps(result, ensure_ascii=False), int(time.time() * 1000))
        return result

    # ---- 步骤 6：上下文 -------------------------------------------------
    def build_context(self, message: IncomingMessage, principal: Principal, can_control: bool) -> dict[str, Any]:
        """客人/群聊上下文不含设备工具；只有主人私聊才挂设备工具。"""
        base: dict[str, Any] = {
            "channel": CONTEXT_CHANNEL,
            "principal": principal.value,
            "message_type": message.message_type.value,
            "group_id": message.group_id,
            "sender_name": message.sender_name,
        }
        if can_control:
            base["tools"] = [DEVICE_TOOL_NAME]
            base["device_tools"] = True
            base["denied_tools"] = []
        else:
            base["tools"] = []
            base["device_tools"] = False
            base["denied_tools"] = list(GUEST_DENIED_TOOLS)
        return base

    # ---- 出站：固定动作 + 幂等 -------------------------------------------
    def deliver_reply(self, message: IncomingMessage, result: dict[str, Any]) -> dict[str, Any]:
        """回复内容只生成一次；重试与重登都复用 message_id 派生的幂等键。"""
        if not result.get("text") and not result.get("media"):
            # 空回复不发送，避免产生无内容消息段。
            return {"status": "skipped", "reason": "empty_reply"}
        if self.sender is None:
            # 未绑定出站发送器（仅本地/测试配置）：保留内容，不视为发送失败。
            result["outbound"] = {"status": "unbound", "reason": "no_onebot_sender"}
            return result["outbound"]
        action = OutboundAction(
            idempotency_key=f"reply:{message.message_id}",
            user_id=message.user_id,
            text=str(result.get("text", "")),
            media=[dict(item) for item in result.get("media", []) if isinstance(item, dict)],
        )
        outcome = self.lifecycle.dispatch(action)
        result["outbound"] = outcome
        if outcome.get("status") not in {SendOutcome.SENT.value, SendOutcome.DUPLICATE.value}:
            # 发送层失败必须给出明确结果，不能伪装成成功回复。
            result["kind"] = "failed"
            result["reason"] = f"onebot_send:{outcome.get('reason', 'unknown')}"
        return outcome

    def execute_plan(
        self,
        message_id: str,
        commands: list[dict[str, Any]],
        *,
        device_task_guard: Any = None,
    ) -> dict[str, Any]:
        guard = device_task_guard if device_task_guard is not None else getattr(self, "device_task_guard", None)
        if guard is not None and not guard():
            return {"kind": "failed", "reason": "message_processing_stopped"}
        if not self.config.signing_secret:
            return {"kind": "failed", "reason": "device_not_paired"}
        try:
            validate_device_plan(commands, self.policy)
        except PlanValidationError as exc:
            return {"kind": "failed", "reason": str(exc)}
        results: list[dict[str, Any]] = []
        for index, item in enumerate(commands):
            if guard is not None and not guard():
                return {"kind": "failed", "results": results, "reason": "message_processing_stopped"}
            envelope = DeviceEnvelope(
                protocol_version=1,
                request_id=str(uuid.uuid4()),
                device_id=self.config.device_id,
                command=item["command"],
                deadline_ms=30_000,
                idempotency_key=f"{message_id}:{index}",
                payload=dict(item["payload"]),
                counter=index + 1,
                issued_at_ms=int(time.time() * 1000),
            )
            envelope = replace(envelope, signature=sign_hmac(envelope.as_dict(), self.config.signing_secret))
            response = self.transport.request(envelope)
            results.append(response.as_dict())
            if response.status != "ok":
                return {"kind": "failed", "results": results, "reason": response.error_code or response.message}
        return {"kind": "device_task", "results": results}

    # ---- 连接生命周期 ---------------------------------------------------
    def on_onebot_online(self, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.lifecycle.record_connection("online", detail=detail)

    def on_onebot_offline(self, reason: str = "") -> dict[str, Any]:
        return self.lifecycle.record_disconnect(reason=reason)

    def recover_after_relogin(self, actions: list[OutboundAction] | None = None) -> dict[str, Any]:
        return self.lifecycle.recover_after_relogin(actions)

    def register_with_astrbot(self, host: Any) -> None:
        """Bind platform callbacks and tools at the AstrBot edge."""
        if not hasattr(host, "register_message_handler"):
            raise TypeError("AstrBot host adapter must expose register_message_handler")
        # 首版连接参数由 AstrBot 管理：这里只注册回调，绝不自行创建第二套 QQ 客户端。
        host.register_message_handler(self.on_onebot_message)
        if hasattr(host, "register_event_handler"):
            host.register_event_handler(self.on_onebot_event)
        if hasattr(host, "register_connection_listener"):
            host.register_connection_listener(self.on_onebot_online, self.on_onebot_offline)


# ==========================================================================
# M03 接线适配层（纯 Python，不 import AstrBot，不持有密钥）
# ==========================================================================
#
# 把 M02 已交付的 `make_model_decider(gateway)` 作为 model_decider 递进插件，
# 并把 `astrbot_adapter` 的四件事嵌进事件链路：
#   1. from_onebot_event   : OneBot 事件 -> IncomingMessage
#   2. to_send_action      : REPLY / SILENT -> OneBot 发送动作（SILENT 只留审计）
#   3. to_device_envelopes : DEVICE_TASK -> DeviceEnvelope
#   4. to_result_message   : 设备响应 -> 固定任务结果消息
# 设计约束：不 import AstrBot（下一阶段接入锁定版）；签名由调用方注入 signer；
# 不 import DeviceTransport（只调用注入的 transport）；不自建 QQ 客户端。


@dataclass
class BridgeOutcome:
    """一次事件桥接的完整结果，供宿主断言与审计。"""

    kind: str
    message_id: str = ""
    principal: str = ""
    routed: dict[str, Any] = field(default_factory=dict)
    send_action: Any = None
    delivery: dict[str, Any] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message_id": self.message_id,
            "principal": self.principal,
            "routed": dict(self.routed),
            "send_action": None if self.send_action is None else self.send_action.as_dict(),
            "delivery": dict(self.delivery),
            "audit": [dict(item) for item in self.audit],
        }


def build_model_decider_for(gateway: Any, persona: Any = None):
    """把 M02 的 `make_model_decider(gateway)` 包成插件可直接用的 model_decider。

    延迟 import：M02 的适配器模块只在真正装配时加载，M03 单测无需它也能跑。
    """
    from .astrbot_adapter import make_model_decider

    if gateway is None:
        raise TypeError("a ModelGateway is required to build the decider")
    return make_model_decider(gateway, persona)


def build_plugin_with_gateway(
    config: PluginConfig,
    gateway: Any,
    transport: DeviceTransport,
    store: TaskStore | None = None,
    *,
    sender: OneBotSender | None = None,
    lifecycle: OneBotLifecycle | None = None,
    persona: Any = None,
    deliver_replies: bool = True,
    defer_device_tasks: bool = False,
) -> WhaleDevicePlugin:
    """一步装配：`make_model_decider(gateway)` 作为 model_decider 注入插件。

    `signer` 不在这里：设备帧签名仍由插件按 `config.signing_secret` 完成，
    密钥由调用方注入，本层不持有、不读取、不落库。
    """
    return WhaleDevicePlugin(
        config,
        build_model_decider_for(gateway, persona),
        transport,
        store,
        sender=sender,
        lifecycle=lifecycle,
        deliver_replies=deliver_replies,
        defer_device_tasks=defer_device_tasks,
    )


class OneBotEventBridge:
    """事件 → 决策 → 发送/设备执行的接线层。

    去重、owner 校验、PrincipalGate、effect 保存仍在 `WhaleDevicePlugin` 里；
    本层只负责把结果转成「标准消息段」或「设备帧」，并做发送/执行。
    """

    def __init__(
        self,
        plugin: WhaleDevicePlugin,
        *,
        sender: OneBotSender | None = None,
        transport: DeviceTransport | None = None,
        signer: Any = None,
        deadline_ms: int | None = None,
    ) -> None:
        self.plugin = plugin
        self.sender = sender if sender is not None else plugin.sender
        self.transport = transport if transport is not None else plugin.transport
        # 签名器由调用方注入；本层没有密钥字段，也不读 config.signing_secret。
        if signer is not None and not callable(signer):
            raise TypeError("signer must be callable")
        self.signer = signer
        self.deadline_ms = deadline_ms

    # ---- 事件入口 ---------------------------------------------------------
    def on_event(self, event: Any) -> BridgeOutcome:
        """OneBot 事件入口：非 message 事件与机器人自发消息仍由插件丢弃。"""
        from .astrbot_adapter import AdapterError, from_onebot_event

        try:
            message = from_onebot_event(event)
        except OneBotEventError as exc:
            return BridgeOutcome(kind="silent", audit=[{"reason": exc.reason}])
        except AdapterError:
            return BridgeOutcome(kind="silent", audit=[{"reason": "adapter_rejected_event"}])
        return self.on_message(message)

    def on_message(self, message: IncomingMessage) -> BridgeOutcome:
        """插件跑完 3-7 步，本层只做「结果 → 段/帧」的转换与投递。"""
        from .astrbot_adapter import AdapterError, audit_for, to_send_action

        decided = self.plugin.on_onebot_message(message)
        routed = dict(decided) if isinstance(decided, dict) else {}
        outcome = BridgeOutcome(
            kind=str(routed.get("kind", "failed")),
            message_id=message.message_id,
            principal=str(routed.get("principal", "")),
            routed=routed,
        )
        audit = audit_for(routed, message)
        if audit is not None:
            outcome.audit.append(
                {
                    "reason": audit.reason,
                    "principal": audit.principal,
                    "message_id": audit.message_id,
                    "contains_device_content": audit.contains_device_content,
                }
            )
        if routed.get("kind") == "device_task":
            outcome.audit.extend(self._execute(message, routed, outcome))
            return outcome
        if self._already_delivered(routed):
            # 插件已自行投递（命中幂等 effect 或未绑定发送器），本层不再发第二次。
            outcome.delivery = dict(routed.get("outbound") or {})
            return outcome
        try:
            outcome.send_action = to_send_action(routed, message)
        except AdapterError as exc:
            # 适配器拒绝（如出现未声明段）时绝不降级成文本，直接判失败。
            outcome.routed["kind"] = "failed"
            outcome.routed["reason"] = f"adapter_error:{exc}"
            outcome.kind = "failed"
            return outcome
        if outcome.send_action is not None:
            outcome.delivery = self.deliver(outcome.send_action, message)
        return outcome

    @staticmethod
    def _already_delivered(routed: dict[str, Any]) -> bool:
        outbound = routed.get("outbound")
        if not isinstance(outbound, dict):
            return False
        return str(outbound.get("status", "")) in {
            SendOutcome.SENT.value,
            SendOutcome.DUPLICATE.value,
            "unbound",
            "skipped",
        }

    def deliver(self, action: Any, message: IncomingMessage) -> dict[str, Any]:
        """把适配器的 `SendAction` 交给 M03 出站层：同一幂等键，网络层最多 3 次。"""
        if self.sender is None or self.plugin.lifecycle is None:
            return {"status": "unbound", "reason": "no_onebot_sender"}
        text = "".join(
            str(segment.data.get("text", "")) for segment in action.segments if segment.type == "text"
        )
        media = [
            {"type": segment.type, **dict(segment.data)}
            for segment in action.segments
            if segment.type != "text"
        ]
        outbound = OutboundAction(
            idempotency_key=f"reply:{message.message_id}",
            user_id=action.user_id,
            text=text,
            media=media,
        )
        return self.plugin.lifecycle.dispatch(outbound)

    def _execute(self, message: IncomingMessage, routed: dict[str, Any], outcome: BridgeOutcome) -> list[dict[str, Any]]:
        """第三、四件事：DEVICE_TASK → DeviceEnvelope → 设备响应 → 固定结果消息。"""
        from .astrbot_adapter import AdapterError, to_device_envelopes, to_result_message

        if self.signer is None:
            if self.plugin.config.signing_secret:
                # 插件已经自己签好名并执行完了（结果里带 results、没有 plan）：
                # 本层不再重复下发，只把已有响应补成固定结果文案。
                return self._summarize_existing(message, routed, outcome)
            outcome.routed["kind"] = "failed"
            outcome.routed["reason"] = "device_not_paired"
            outcome.kind = "failed"
            return [{"reason": "device_not_paired"}]
        if self.transport is None:
            outcome.routed["kind"] = "failed"
            outcome.routed["reason"] = "no_device_transport"
            outcome.kind = "failed"
            return [{"reason": "no_device_transport"}]
        try:
            envelopes = to_device_envelopes(
                routed,
                message,
                self.plugin.config.device_id,
                deadline_ms=self.deadline_ms,
                signer=self.signer,
            )
        except (AdapterError, KeyError) as exc:
            outcome.routed["kind"] = "failed"
            outcome.routed["reason"] = f"adapter_error:{exc.__class__.__name__}"
            outcome.kind = "failed"
            return [{"reason": outcome.routed["reason"]}]
        results: list[dict[str, Any]] = []
        result_messages: list[dict[str, Any]] = []
        for envelope in envelopes:
            response = self.transport.request(envelope)
            results.append(response.as_dict())
            # 第四件事：设备响应 -> 固定任务结果消息（不拼接设备原始文本）。
            result_messages.append(to_result_message(response, envelope.command))
            if response.status != "ok":
                outcome.routed["kind"] = "failed"
                outcome.routed["reason"] = response.error_code or "device_failed"
                outcome.kind = "failed"
                break
        if results:
            outcome.routed["results"] = results
        if result_messages:
            outcome.routed["result_messages"] = result_messages
        return results

    def _summarize_existing(self, message: IncomingMessage, routed: dict[str, Any], outcome: BridgeOutcome) -> list[dict[str, Any]]:
        """插件已执行完时，只把已有设备响应转成固定结果文案，绝不再下发命令。"""
        from .astrbot_adapter import to_result_message

        existing = routed.get("results")
        if not isinstance(existing, list):
            return [{"reason": "delegated_to_plugin_execute_plan"}]
        result_messages: list[dict[str, Any]] = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            response = DeviceResponse(
                request_id=str(item.get("request_id", "")),
                device_id=str(item.get("device_id", "")),
                status=str(item.get("status", "")),
                error_code=str(item.get("error_code", "")),
                message=str(item.get("message", "")),
            )
            result_messages.append(to_result_message(response, str(item.get("command", ""))))
        if result_messages:
            outcome.routed["result_messages"] = result_messages
        return [{"reason": "delegated_to_plugin_execute_plan"}]

    # ---- 注册 -------------------------------------------------------------
    def register(self, host: Any) -> None:
        """只注册回调。连接参数由 AstrBot 管理，本层不建客户端、不连 socket。"""
        if not hasattr(host, "register_message_handler"):
            raise TypeError("AstrBot host adapter must expose register_message_handler")
        host.register_message_handler(self.on_message)
        if hasattr(host, "register_event_handler"):
            host.register_event_handler(self.on_event)
        if hasattr(host, "register_connection_listener"):
            host.register_connection_listener(self.plugin.on_onebot_online, self.plugin.on_onebot_offline)


def build_bridge(
    config: PluginConfig,
    gateway: Any,
    transport: DeviceTransport,
    store: TaskStore | None = None,
    *,
    sender: OneBotSender | None = None,
    lifecycle: OneBotLifecycle | None = None,
    persona: Any = None,
    signer: Any = None,
    deadline_ms: int | None = None,
) -> tuple[WhaleDevicePlugin, OneBotEventBridge]:
    """一步装配插件 + 桥接层。插件交出投递与执行，由桥接层按 adapter 规则完成。"""
    plugin = build_plugin_with_gateway(
        config,
        gateway,
        transport,
        store,
        sender=sender,
        lifecycle=lifecycle,
        persona=persona,
        deliver_replies=False,
        defer_device_tasks=True,
    )
    bridge = OneBotEventBridge(
        plugin,
        sender=sender,
        transport=transport,
        signer=signer,
        deadline_ms=deadline_ms,
    )
    return plugin, bridge
