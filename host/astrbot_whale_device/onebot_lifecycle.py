from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .onebot import OneBotMediaError, OneBotSender
from .storage import TaskStore

#: 连接状态只有在线/离线两态；掉线时主机显示离线。
CONNECTION_STATES = frozenset({"online", "offline"})
#: 一次发送最多尝试 3 次，且每次都用同一个消息幂等键。
DEFAULT_MAX_ATTEMPTS = 3
#: 只重试「网络层」故障；解析/素材错误重试多少次都没用。
RETRYABLE_NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError)

CONNECTION_KEY_PREFIX = "onebot:connection:"
AUDIT_KEY_PREFIX = "onebot:audit:"
OUTBOUND_KEY_PREFIX = "onebot:outbound:"
PENDING_KEY_PREFIX = "onebot:pending:"
PENDING_ACTION_PREFIX = "onebot:pending_action:"


class SendOutcome(str, Enum):
    SENT = "sent"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    REJECTED = "rejected"


class OutboundError(RuntimeError):
    """发送层永久失败（非网络层），不参与重试。"""


@dataclass(frozen=True)
class OutboundAction:
    """一条固定出站动作；`idempotency_key` 在整个重试过程中保持不变。"""

    idempotency_key: str
    user_id: str
    text: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "user_id": self.user_id,
            "text": self.text,
            "media": [dict(item) for item in self.media],
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


class OneBotLifecycle:
    """OneBot 连接生命周期与出站发送的唯一入口。

    - 连接状态：写入 `devices`/主机状态 + 审计（经 TaskStore 公开 API 落库）。
    - 发送：网络层失败最多重试 3 次，全部使用同一幂等键；已发送的键不会重发。
    - 重新登录：先查已发送 effect，再补发未发送动作；绝不重新调用模型。
    """

    def __init__(
        self,
        store: TaskStore,
        device_id: str,
        sender: OneBotSender | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not device_id:
            raise ValueError("device_id is required")
        self.store = store
        self.device_id = device_id
        self.sender = sender
        self.max_attempts = max_attempts
        self._clock = clock or _now_ms
        # 待发送/待恢复的固定动作（内存队列；可在重登后重放）。
        self._pending: dict[str, OutboundAction] = {}
        # 进程内审计流（TaskStore 目前没有 audit_events 写入 API，只落 effects）。
        self._audit_log: list[dict[str, Any]] = []

    # ---- 持久化小工具（只用 TaskStore 现有公开 API）-----------------------
    @property
    def connection_key(self) -> str:
        return f"{CONNECTION_KEY_PREFIX}{self.device_id}"

    def _read(self, key: str) -> dict[str, Any] | None:
        raw = self.store.effect(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _write(self, key: str, value: dict[str, Any]) -> None:
        self.store.save_effect(key, json.dumps(value, ensure_ascii=False), self._clock())

    def _audit(self, event_type: str, actor_user_id: str, message_id: str, detail: dict[str, Any]) -> str:
        key = f"{AUDIT_KEY_PREFIX}{uuid.uuid4().hex}"
        record = {
            "event_type": event_type,
            "actor_user_id": actor_user_id,
            "message_id": message_id,
            "device_id": self.device_id,
            "detail": detail,
            "created_at": self._clock(),
        }
        self._write(key, record)
        self._audit_log.append(record)
        return key

    def audit_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type is None:
            return [dict(item) for item in self._audit_log]
        return [dict(item) for item in self._audit_log if item.get("event_type") == event_type]

    # ---- 连接状态（审计 + devices/主机状态）-------------------------------
    def record_connection(
        self,
        state: str,
        *,
        detail: dict[str, Any] | None = None,
        actor_user_id: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        """把 OneBot 连接状态写进主机状态与审计。state 只能是 online / offline。"""
        if state not in CONNECTION_STATES:
            raise ValueError(f"unsupported connection state: {state!r}")
        record = {
            "device_id": self.device_id,
            "connection": "onebot",
            "state": state,
            "detail": dict(detail or {}),
            "updated_at": self._clock(),
        }
        self._write(self.connection_key, record)
        self._audit("onebot.connection", actor_user_id, message_id, record)
        if state == "online" and str((detail or {}).get("reason", "")) == "relogin":
            # M13 验收矩阵点名的审计事件：重新登录。
            self._audit("onebot_relogin", actor_user_id, message_id, record)
        return record

    def connection_state(self) -> dict[str, Any] | None:
        return self._read(self.connection_key)

    def is_online(self) -> bool:
        state = self.connection_state()
        return bool(state) and state.get("state") == "online"

    # ---- 出站动作 -------------------------------------------------------
    @property
    def _pending_index(self) -> str:
        return f"{PENDING_KEY_PREFIX}{self.device_id}"

    def _outbound_key(self, idempotency_key: str) -> str:
        return f"{OUTBOUND_KEY_PREFIX}{idempotency_key}"

    def _action_key(self, idempotency_key: str) -> str:
        return f"{PENDING_ACTION_PREFIX}{idempotency_key}"

    def _mark_pending(self, action: OutboundAction) -> None:
        self._pending[action.idempotency_key] = action
        # 落库动作本体，重登后即使进程重启也能恢复固定出站动作（不重跑模型）。
        self._write(self._action_key(action.idempotency_key), {"action": action.payload(), "updated_at": self._clock()})
        queued = self._read(self._pending_index) or {"keys": []}
        keys = [str(k) for k in queued.get("keys", []) if isinstance(k, str)]
        if action.idempotency_key not in keys:
            keys.append(action.idempotency_key)
        self._write(self._pending_index, {"device_id": self.device_id, "keys": keys, "updated_at": self._clock()})

    def _clear_pending(self, idempotency_key: str) -> None:
        self._pending.pop(idempotency_key, None)
        queued = self._read(self._pending_index) or {"keys": []}
        keys = [str(k) for k in queued.get("keys", []) if isinstance(k, str) and k != idempotency_key]
        self._write(self._pending_index, {"device_id": self.device_id, "keys": keys, "updated_at": self._clock()})

    def pending_keys(self) -> list[str]:
        queued = self._read(self._pending_index) or {}
        keys = [str(k) for k in queued.get("keys", []) if isinstance(k, str)]
        for key in self._pending:
            if key not in keys:
                keys.append(key)
        return keys

    def sent_record(self, idempotency_key: str) -> dict[str, Any] | None:
        """查询某条消息是否已发送（重登恢复前必须先问这一句）。"""
        record = self._read(self._outbound_key(idempotency_key))
        return record if record and record.get("status") == SendOutcome.SENT.value else None

    def dispatch(self, action: OutboundAction) -> dict[str, Any]:
        """发送一条固定动作：同幂等键最多 3 次，已发送则直接返回既有结果。"""
        if not action.idempotency_key:
            raise ValueError("idempotency_key is required")
        existing = self.sent_record(action.idempotency_key)
        if existing is not None:
            self._clear_pending(action.idempotency_key)
            return {"status": SendOutcome.DUPLICATE.value, "idempotency_key": action.idempotency_key, "sent": existing}
        if self.sender is None:
            raise OutboundError("no OneBot sender is bound")
        # 已明确记录为离线 → 不空转重试，记账后交给重登恢复。
        # 从未记录连接状态时交给平台回调自身处理（不凭空判离线）。
        state = self.connection_state()
        if state is not None and state.get("state") == "offline":
            self._mark_pending(action)
            return {"status": SendOutcome.FAILED.value, "idempotency_key": action.idempotency_key, "attempts": 0, "reason": "onebot_offline"}

        attempts = 0
        last_reason = ""
        network_failures = 0
        while attempts < self.max_attempts:
            attempts += 1
            try:
                # 每一次重试都带同一个幂等键，平台侧可据此去重。
                result = self.sender.send_private(
                    action.user_id,
                    action.text,
                    action.media,
                    idempotency_key=action.idempotency_key,
                )
            except OneBotMediaError as exc:
                # 素材/段非法：重试无意义，直接判定拒绝。
                self._audit("onebot.send_rejected", action.user_id, action.idempotency_key, {"reason": str(exc)})
                return {"status": SendOutcome.REJECTED.value, "idempotency_key": action.idempotency_key, "attempts": attempts, "reason": str(exc)}
            except RETRYABLE_NETWORK_ERRORS as exc:
                network_failures += 1
                last_reason = exc.__class__.__name__
                continue
            except Exception as exc:  # 平台回调抛非网络异常：不重试
                self._audit("onebot.send_failed", action.user_id, action.idempotency_key, {"reason": exc.__class__.__name__})
                return {"status": SendOutcome.FAILED.value, "idempotency_key": action.idempotency_key, "attempts": attempts, "reason": exc.__class__.__name__}
            sent = {
                "status": SendOutcome.SENT.value,
                "idempotency_key": action.idempotency_key,
                "attempts": attempts,
                "retries": attempts - 1,
                "message_type": "private",
                "user_id": action.user_id,
                "sent_at": self._clock(),
                "platform_result": result if isinstance(result, (str, int, float, bool, dict, list)) or result is None else str(result),
            }
            self._write(self._outbound_key(action.idempotency_key), sent)
            self._clear_pending(action.idempotency_key)
            self._audit("onebot.send_sent", action.user_id, action.idempotency_key, {"attempts": attempts})
            return dict(sent)

        self._mark_pending(action)
        self._audit("onebot.send_failed", action.user_id, action.idempotency_key, {"reason": last_reason, "network_failures": network_failures})
        return {
            "status": SendOutcome.FAILED.value,
            "idempotency_key": action.idempotency_key,
            "attempts": attempts,
            "reason": last_reason or "network_error",
        }

    # ---- 重新登录恢复 ----------------------------------------------------
    def recover_after_relogin(self, actions: list[OutboundAction] | None = None) -> dict[str, Any]:
        """先查询已发送 effect，再补发未发送的固定出站动作；不重跑模型。"""
        self.record_connection("online", detail={"reason": "relogin"})
        index = {action.idempotency_key: action for action in (actions or [])}
        for key in self.pending_keys():
            if key in index:
                continue
            stored = self._read(self._action_key(key)) or {}
            if isinstance(stored.get("action"), dict):
                try:
                    index[key] = OutboundAction(**stored["action"])
                except TypeError:
                    continue
        already_sent: list[str] = []
        recovered: list[dict[str, Any]] = []
        still_pending: list[str] = []
        for key, action in index.items():
            existing = self.sent_record(key)
            if existing is not None:
                self._clear_pending(key)
                already_sent.append(key)
                # M13 验收矩阵点名的审计事件：已发送 effect 被回放，不重发。
                self._audit("effect_replayed", action.user_id, key, {"sent_at": existing.get("sent_at")})
                continue
            outcome = self.dispatch(action)
            if outcome["status"] == SendOutcome.SENT.value:
                recovered.append(key)
            else:
                still_pending.append(key)
        return {
            "status": "relogin_recovered",
            "already_sent": sorted(already_sent),
            "recovered": sorted(recovered),
            "still_pending": sorted(still_pending),
        }

    def record_disconnect(self, *, reason: str = "", actor_user_id: str = "") -> dict[str, Any]:
        return self.record_connection("offline", detail={"reason": reason}, actor_user_id=actor_user_id)
