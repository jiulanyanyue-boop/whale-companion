from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_livingmemory.core import utils


@pytest.mark.asyncio
async def test_event_persona_precedes_stale_session_persona(monkeypatch):
    lookup = AsyncMock(return_value={"persona_id": "whale_girl"})
    monkeypatch.setattr(utils.sp, "get_async", lookup)
    for persona in ("whale_owner", "whale_group"):
        event = SimpleNamespace(
            unified_msg_origin="whale_qq:GroupMessage:test",
            get_extra=lambda key: persona,
        )
        assert await utils.get_persona_id(SimpleNamespace(), event) == persona
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_session_setting_falls_back_to_conversation(monkeypatch):
    monkeypatch.setattr(utils.sp, "get_async", AsyncMock(return_value=None))
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="chat"),
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(persona_id="whale_group")
        ),
    )
    event = SimpleNamespace(unified_msg_origin="test", get_extra=lambda key: None)
    assert (
        await utils.get_persona_id(SimpleNamespace(conversation_manager=manager), event)
        == "whale_group"
    )
