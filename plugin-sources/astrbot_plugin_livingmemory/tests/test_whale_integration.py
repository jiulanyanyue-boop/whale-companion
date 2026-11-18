from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from astrbot_plugin_livingmemory.main import LivingMemoryPlugin
from astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_livingmemory.core import utils
from astrbot_plugin_livingmemory.core.whale_short_term import save_topic, read_topics, clear_topics

def event(sender='1', group='', text='/记忆公开 公共偏好', platform='whale_qq'):
    return SimpleNamespace(message_str=text, unified_msg_origin=f'{platform}:GroupMessage:{group}' if group else f'{platform}:FriendMessage:{sender}',
        get_platform_id=lambda:platform, get_platform_name=lambda:'aiocqhttp',
        get_sender_id=lambda:sender, get_group_id=lambda:group, plain_result=lambda text:text)

@pytest.mark.asyncio
async def test_only_owner_private_can_publish_global_memory(monkeypatch):
    monkeypatch.setenv('WHALE_OWNER_ID','1')
    monkeypatch.setattr(utils,'get_persona_id',AsyncMock(return_value='whale_girl'))
    engine=SimpleNamespace(add_memory=AsyncMock(return_value=42))
    plugin=SimpleNamespace(context=None, config_manager={}, initializer=SimpleNamespace(memory_engine=engine),_ensure_plugin_ready=AsyncMock(return_value=(True,'')))
    for rejected in [event(sender='2'), event(group='8'),event(platform='other_qq')]:
        assert [x async for x in LivingMemoryPlugin.whale_publish_memory(plugin,rejected)]
    engine.add_memory.assert_not_awaited()
    assert [x async for x in LivingMemoryPlugin.whale_publish_memory(plugin,event())]
    assert engine.add_memory.call_args.kwargs['session_id']=='livingmemory:global'

@pytest.mark.asyncio
async def test_group_member_profile_uses_trusted_sender_and_group(monkeypatch):
    monkeypatch.setenv('WHALE_OWNER_ID','1')
    monkeypatch.setattr(utils,'get_persona_id',AsyncMock(return_value='whale_girl'))
    engine=SimpleNamespace(add_memory=AsyncMock(return_value=43))
    plugin=SimpleNamespace(context=None,config_manager={},initializer=SimpleNamespace(memory_engine=engine),_ensure_plugin_ready=AsyncMock(return_value=(True,'')))
    current=event(sender='2',group='8',text='/群内记住 我喜欢米饭')
    assert [x async for x in LivingMemoryPlugin.whale_group_member_memory(plugin,current)]
    assert engine.add_memory.call_args.kwargs['session_id']=='livingmemory:member:aiocqhttp:8:2'

def test_short_summary_is_scoped_and_deleted(monkeypatch,tmp_path):
    monkeypatch.setenv('ASTRBOT_ROOT',str(tmp_path))
    save_topic('owner','p','私聊内容');save_topic('group','p','群内容')
    assert read_topics(('group',),'p')=='群内容'
    clear_topics('group')
    assert read_topics(('group',),'p')==''
    assert read_topics(('owner',),'p')=='私聊内容'

def test_two_memory_layers_share_hard_token_budget():
    import tiktoken
    encoder=tiktoken.get_encoding('cl100k_base')
    memory,topic=MemoryRecall._apply_whale_token_budget('𠮷图片偏好'*5000,'最近话题'*1000,1500)
    assert len(encoder.encode(memory))+len(encoder.encode(topic))<=1500
