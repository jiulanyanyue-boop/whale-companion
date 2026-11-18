from types import SimpleNamespace

from astrbot_plugin_livingmemory.core.whale_memory_policy import (
    PUBLIC_SCOPE, is_non_chat_event, memory_scope_access, may_read_scope,
    resolve_policy, search_allowed_scopes,
)


def event(sender="user", group="", session="qq:private:s"):
    return SimpleNamespace(
        unified_msg_origin=session, sender_id=sender, platform="aiocqhttp",
        group_id=group, get_sender_id=lambda: sender,
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: group,
    )

def test_private_and_owner_scopes_are_isolated():
    owner = resolve_policy(event("owner"), "owner")
    stranger = resolve_policy(event("stranger"), "owner")
    assert owner.allowed_scopes == (PUBLIC_SCOPE, "livingmemory:owner:aiocqhttp:owner")
    assert stranger.allowed_scopes == (PUBLIC_SCOPE, "livingmemory:private:aiocqhttp:stranger")
    assert owner.current_scope != stranger.current_scope

def test_group_never_includes_private_scope_and_member_ids_are_stable():
    one = resolve_policy(event("u1", "g1", "qq:group:g1"), "owner")
    two = resolve_policy(event("u2", "g1", "qq:group:g1"), "owner")
    assert one.current_scope == "livingmemory:group:aiocqhttp:g1"
    assert one.allowed_scopes == (PUBLIC_SCOPE, "livingmemory:group:aiocqhttp:g1", "livingmemory:member:aiocqhttp:g1:u1")
    assert two.allowed_scopes[-1] == "livingmemory:member:aiocqhttp:g1:u2"
    assert all("private" not in scope and "owner" not in scope for scope in one.allowed_scopes)

def test_unknown_event_fails_closed():
    assert resolve_policy(SimpleNamespace(), "owner") is None

def test_scope_context_is_task_local_and_restored():
    with memory_scope_access(("public", "private")):
        assert may_read_scope("public") and may_read_scope("private")
        assert not may_read_scope("secret")
    assert not may_read_scope("secret")

def test_synthetic_sources_are_skipped():
    assert is_non_chat_event(SimpleNamespace(event_metadata={"source": "cron"}))
    assert is_non_chat_event(SimpleNamespace(source="background_task"))
    assert is_non_chat_event(SimpleNamespace(device_tool_result=True))
    assert is_non_chat_event(SimpleNamespace(get_extra=lambda key: "device_tool_result" if key == "source" else None))
    assert not is_non_chat_event(event())



def test_group_profile_scope_is_readable_only_for_same_member():
    one = resolve_policy(event("u1", "g1", "qq:group:g1"), "owner")
    two = resolve_policy(event("u2", "g1", "qq:group:g1"), "owner")
    with memory_scope_access(one.allowed_scopes):
        assert may_read_scope(one.allowed_scopes[-1])
        assert not may_read_scope(two.allowed_scopes[-1])
