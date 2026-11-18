"""Whale-specific memory isolation policy for trusted AstrBot events."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

PUBLIC_SCOPE = "livingmemory:global"
_allowed_scopes: ContextVar[tuple[str, ...] | None] = ContextVar(
    "whale_memory_allowed_scopes", default=None
)


@dataclass(frozen=True)
class MemoryPolicy:
    current_scope: str
    allowed_scopes: tuple[str, ...]
    is_group: bool
    is_owner: bool


def _value(event: Any, method: str, attr: str = "") -> str:
    getter = getattr(event, method, None)
    try:
        result = getter() if callable(getter) else getattr(event, attr or method, "")
    except Exception:
        return ""
    return str(result or "").strip()


def resolve_policy(event: Any, owner_id: str | None = None) -> MemoryPolicy | None:
    """Resolve scopes solely from AstrBot's current event object."""
    session = _value(event, "unified_msg_origin", "unified_msg_origin")
    sender = _value(event, "get_sender_id", "sender_id")
    platform = _value(event, "get_platform_name", "platform")
    group = _value(event, "get_group_id", "group_id")
    owner = str(owner_id if owner_id is not None else os.getenv("WHALE_OWNER_ID", "")).strip()
    if not session or not sender or not platform:
        return None
    owner_match = platform.casefold() == "aiocqhttp" and bool(owner) and sender == owner
    if group:
        group_scope = f"livingmemory:group:{platform}:{group}"
        member_scope = f"livingmemory:member:{platform}:{group}:{sender}"
        return MemoryPolicy(group_scope, (PUBLIC_SCOPE, group_scope, member_scope), True, owner_match)
    private_scope = f"livingmemory:private:{platform}:{sender}"
    scopes = (PUBLIC_SCOPE, private_scope)
    if owner_match:
        private_scope = f"livingmemory:owner:{platform}:{sender}"
        scopes = (PUBLIC_SCOPE, private_scope)
    return MemoryPolicy(private_scope, scopes, False, owner_match)


def resolve_allowed_scopes(event: Any, owner_id: str | None = None) -> tuple[str, ...]:
    policy = resolve_policy(event, owner_id)
    return policy.allowed_scopes if policy else ()


def current_allowed_scopes() -> tuple[str, ...] | None:
    return _allowed_scopes.get()


def policy_enabled() -> bool:
    return bool(os.getenv("WHALE_OWNER_ID", "").strip())


def policy_requested() -> bool:
    value = os.getenv("WHALE_MEMORY_POLICY", "").strip().casefold()
    if value in {"0", "false", "off", "disabled"}:
        return False
    return value in {"1", "true", "on", "enabled"} or policy_enabled()


@contextmanager
def memory_scope_access(scopes: tuple[str, ...] | list[str]):
    normalized = tuple(dict.fromkeys(str(scope) for scope in scopes if scope))
    token = _allowed_scopes.set(normalized)
    try:
        yield
    finally:
        _allowed_scopes.reset(token)


def may_read_scope(scope: str | None) -> bool:
    allowed = current_allowed_scopes()
    return allowed is not None and bool(scope and scope in allowed)


def is_non_chat_event(event: Any) -> bool:
    """Reject synthetic/background events by class and trusted metadata."""
    if event is None or type(event).__name__ == "AgentWakeupEvent":
        return True
    metadata = getattr(event, "event_metadata", None)
    if not isinstance(metadata, dict):
        metadata = getattr(event, "_event_metadata", None)
    blocked = {"cron", "background_task", "plugin"}
    if isinstance(metadata, dict) and str(metadata.get("source", "")).casefold() in blocked:
        return True
    if isinstance(getattr(event, "source", None), str) and event.source.casefold() in blocked:
        return True
    if getattr(event, "device_tool_result", False) is True:
        return True
    if isinstance(metadata, dict) and metadata.get("device_tool_result") is True:
        return True
    get_extra = getattr(event, "get_extra", None)
    if callable(get_extra):
        try:
            if get_extra("device_tool_result") is True:
                return True
        except Exception:
            pass
        try:
            extra_source = str(get_extra("source") or "").casefold()
            if extra_source in blocked or extra_source == "device_tool_result":
                return True
        except Exception:
            pass
    return False


async def search_allowed_scopes(engine: Any, query: str, k: int, persona_id: str | None):
    """Search each authorized scope before merging under one result budget."""
    scopes = current_allowed_scopes()
    if scopes is None:
        raise RuntimeError("scope access context is required")
    found = {}
    for scope in scopes:
        rows = await engine.search_memories(
            query=query, k=k, session_id=scope, persona_id=persona_id
        )
        for row in rows:
            found[row.doc_id] = row
    return sorted(found.values(), key=lambda row: row.final_score, reverse=True)[:k]
