import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from autonomy import (
    ActivityEvent,
    ActivityState,
    PersonalActivities,
    background_plan_error,
    interactive_targets,
    parse_decision,
)


def evidence():
    return {
        "snapshot_id": "snap",
        "package_name": "social.app",
        "nodes": "target64|hash24|View|一个想看的故事||0,0,100,100|true|false|false\n"
        "unsafe|hash25|View|发布||0,0,100,100|true|false|false",
    }


def test_node_payload_keeps_ids_in_original_order():
    target = interactive_targets(evidence())[0]
    assert target["action_payload"] == {
        "snapshot_id": "snap",
        "expected_package": "social.app",
        "target_id": "target64",
        "selector_hash": "hash24",
    }


def test_self_activity_rejects_mutations_and_unknown_targets():
    event = SimpleNamespace(get_extra=lambda *a: evidence())
    assert background_plan_error(
        [{"command": "device.ui.input", "payload": {"text": "abc"}}], event
    )
    assert background_plan_error([{"command": "device.ui.tap", "payload": {}}], event)
    targets = interactive_targets(evidence())
    assert background_plan_error(
        [{"command": "device.ui.tap", "payload": targets[1]["action_payload"]}], event
    )
    assert not background_plan_error(
        [{"command": "device.ui.tap", "payload": targets[0]["action_payload"]}], event
    )


def test_decision_cannot_set_arbitrary_recipient_or_infinite_frequency():
    decision = {
        "summary": "读到一条故事",
        "message": "",
        "meme_query": "",
        "next_minutes": 0,
    }
    assert parse_decision(json.dumps(decision))["next_minutes"] == 10
    with pytest.raises(ValueError):
        parse_decision(json.dumps(decision | {"recipient": "other"}))
    with pytest.raises(ValueError):
        parse_decision(json.dumps(decision | {"next_minutes": True}))


def test_contact_reservations_survive_reload_and_bound_daily_sends(tmp_path):
    path = tmp_path / "state.json"
    state = ActivityState(path)
    assert state.reserve_contact(10000, "2026-11-09", 2, 900)
    state = ActivityState(path)
    assert not state.reserve_contact(10001, "2026-11-09", 2, 900)
    assert state.reserve_contact(11000, "2026-11-09", 2, 900)
    assert not state.reserve_contact(13000, "2026-11-09", 2, 900)
    assert state.reserve_contact(20000, "2026-11-10", 2, 900)


def make_activities(tmp_path, now=None):
    stamp = now or datetime(2026, 11, 9, 4, tzinfo=timezone.utc).timestamp()
    plugin = SimpleNamespace(
        config={"autonomy_enabled": True, "autonomy_timezone": "America/New_York"},
        platform_id="p",
        owner_user_id="owner",
        context=SimpleNamespace(),
    )
    return PersonalActivities(plugin, tmp_path / "state.json", lambda: stamp)


def test_quiet_hours_use_configured_timezone(tmp_path):
    activities = make_activities(
        tmp_path, datetime(2026, 11, 9, 18, tzinfo=timezone.utc).timestamp()
    )
    assert (
        not activities.in_active_hours()
    )  # 02:00 Beijing, regardless of the stored legacy timezone.


def test_owner_return_invalidates_background_before_further_tools(tmp_path):
    activities = make_activities(tmp_path)
    event = ActivityEvent(activities.plugin, activities.generation)
    assert activities.current(event)
    activities.note_owner()
    assert not activities.current(event)
    assert activities.state.data["next_at"] > activities.clock()


def test_no_model_call_when_disabled_waiting_or_quiet(tmp_path):
    activities = make_activities(tmp_path)
    activities.think = AsyncMock()
    assert asyncio.run(activities.run_once())["status"] == "waiting"
    activities.plugin.config["autonomy_enabled"] = False
    assert asyncio.run(activities.run_once())["status"] == "disabled_or_busy"
    activities.think.assert_not_awaited()


def test_no_send_when_owner_returns_during_thinking(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.deliver = AsyncMock()

        async def think(event, receipts):
            activities.note_owner()
            return {
                "summary": "reading",
                "message": "hello",
                "meme_query": "",
                "next_minutes": 15,
            }

        activities.think = think
        assert (await activities.run_once(dry_run=True, force=True))[
            "status"
        ] == "owner_resumed"
        activities.deliver.assert_not_awaited()

    asyncio.run(check())


def test_rest_is_recorded_without_contact_and_restarts_continue(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["next_at"] = 0
        activities.think = AsyncMock(
            return_value={
                "summary": "今天先休息，下次继续猫咪的话题",
                "message": "",
                "meme_query": "",
                "next_minutes": 20,
            }
        )
        activities.deliver = AsyncMock()
        assert (await activities.run_once())["sent"] is False
        activities.deliver.assert_not_awaited()
        reloaded = make_activities(tmp_path)
        assert "猫咪" in reloaded.context_note()
        assert (await reloaded.run_once())["status"] == "waiting"

    asyncio.run(check())


def test_force_cannot_send_live(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(make_activities(tmp_path).run_once(force=True))


def test_pause_blocks_old_and_future_background_events(tmp_path):
    activities = make_activities(tmp_path)
    activities.pause()
    assert not activities.current(
        ActivityEvent(activities.plugin, activities.generation)
    )
    activities.pause(0)
    assert activities.current(ActivityEvent(activities.plugin, activities.generation))


def test_named_nonclickable_content_can_use_gesture_fallback():
    observed = {
        "snapshot_id": "s",
        "package_name": "social.app",
        "nodes": "card|hash|FrameLayout||笔记：我的软装前后对比|0,0,200,200|false|false|false",
    }
    target = interactive_targets(observed)[0]
    assert not target["clickable"]
    event = SimpleNamespace(get_extra=lambda *a: observed)
    assert not background_plan_error(
        [{"command": "device.ui.tap", "payload": target["action_payload"]}], event
    )


def test_only_observed_search_fields_accept_background_text():
    def plan_for(label):
        observed = {
            "snapshot_id": "s",
            "package_name": "social.app",
            "nodes": f"field|hash|EditText|{label}||0,0,200,200|true|true|false",
        }
        target = interactive_targets(observed)[0]
        event = SimpleNamespace(get_extra=lambda *a: observed)
        return background_plan_error(
            [
                {
                    "command": "device.ui.input",
                    "payload": target["action_payload"] | {"text": "可爱猫咪"},
                }
            ],
            event,
        )

    assert not plan_for("搜索")
    assert plan_for("写评论")
    assert plan_for("请输入密码")


def test_confirmed_contact_and_cooldown_share_same_send_boundary(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.plugin.context.send_message = AsyncMock(return_value=True)
        event = ActivityEvent(activities.plugin, activities.generation)
        decision = {
            "summary": "休息",
            "message": "刚想起个小事",
            "meme_query": "",
            "next_minutes": 20,
        }
        assert (await activities.deliver(event, decision, dry_run=False))[
            "sent"
        ] is True
        assert (await activities.deliver(event, decision, dry_run=False))[
            "delivery"
        ] == "cooldown_or_quota"
        activities.plugin.context.send_message.assert_awaited_once()

    asyncio.run(check())


def test_delivery_capture_has_no_network_or_quota_effect(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.plugin.context.send_message = AsyncMock()
        decision = {
            "summary": "",
            "message": "我来找你玩啦",
            "meme_query": "",
            "next_minutes": 20,
        }
        event = ActivityEvent(activities.plugin, activities.generation)
        assert (await activities.deliver(event, decision, dry_run=True))[
            "delivery"
        ] == "captured"
        activities.plugin.context.send_message.assert_not_awaited()
        assert "contacts" not in activities.state.data

    asyncio.run(check())


def test_uncertain_delivery_is_reserved_not_retried(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.plugin.context.send_message = AsyncMock(return_value=False)
        event = ActivityEvent(activities.plugin, activities.generation)
        decision = {
            "summary": "",
            "message": "一条发现",
            "meme_query": "",
            "next_minutes": 20,
        }
        assert (await activities.deliver(event, decision, dry_run=False))[
            "delivery"
        ] == "unconfirmed_no_retry"
        reloaded = make_activities(tmp_path)
        reloaded.plugin.context.send_message = AsyncMock()
        event = ActivityEvent(reloaded.plugin, reloaded.generation)
        assert (await reloaded.deliver(event, decision, dry_run=False))[
            "delivery"
        ] == "cooldown_or_quota"
        reloaded.plugin.context.send_message.assert_not_awaited()

    asyncio.run(check())


def test_internal_activity_uses_the_real_framework_event_type(tmp_path):
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.star.context import Context

    activities = make_activities(tmp_path)
    event = ActivityEvent(activities.plugin, 0)
    context = AstrAgentContext(context=object.__new__(Context), event=event)
    assert context.event.get_sender_id() == "owner"
    assert context.event.unified_msg_origin == "p:FriendMessage:owner"
    assert context.event.trace is not None


def test_beijing_time_ignores_legacy_config_and_machine_timezone(tmp_path, monkeypatch):
    activities = make_activities(tmp_path)
    activities.plugin.config["autonomy_timezone"] = "America/New_York"
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    assert activities.local_now().hour == 12
    assert activities.local_now().utcoffset().total_seconds() == 8 * 3600


def test_quiet_hours_allow_private_browsing_when_selected(tmp_path):
    async def check():
        activities = make_activities(
            tmp_path, datetime(2026, 11, 9, 18, tzinfo=timezone.utc).timestamp()
        )
        activities.state.data["next_at"] = 0
        activities.think = AsyncMock(
            return_value={
                "summary": "自己看了一会，不打扰主人",
                "message": "",
                "meme_query": "",
                "next_minutes": 15,
            }
        )
        assert not activities.in_active_hours()
        assert (await activities.run_once())["status"] == "completed"
        activities.plugin.config["autonomy_browse_during_quiet"] = False
        activities.state.data["next_at"] = 0
        activities.think.reset_mock()
        assert (await activities.run_once())["status"] == "waiting"
        activities.think.assert_not_awaited()

    asyncio.run(check())


def test_private_browsing_at_night_cannot_send_a_message(tmp_path):
    async def check():
        activities = make_activities(
            tmp_path, datetime(2026, 11, 9, 18, tzinfo=timezone.utc).timestamp()
        )
        activities.plugin.context.send_message = AsyncMock()
        event = ActivityEvent(activities.plugin, 0)
        result = await activities.deliver(
            event, {"message": "这条不能夜里发", "meme_query": ""}, dry_run=False
        )
        assert result["delivery"] == "postponed"
        activities.plugin.context.send_message.assert_not_awaited()

    asyncio.run(check())


def test_run_now_uses_worker_and_preserves_contact_quota(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["contacts"] = [activities.clock()]
        assert activities.request_now()
        assert activities.state.data["next_at"] == activities.clock()
        assert activities.state.data["contacts"] == [activities.clock()]
        assert activities.status()["worker_alive"]
        activities.running = True
        assert not activities.request_now()
        await activities.stop()

    asyncio.run(check())


def test_budget_wrapup_is_a_checkpoint_and_not_a_contact(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["next_at"] = 0
        activities.deliver = AsyncMock()

        async def think(event, receipts):
            event.set_extra("whale_activity_tool_calls", 12)
            receipts.append({"command": "device.ui.tap", "status": "ok"})
            return {
                "summary": "刚点进笔记，下一步读正文",
                "message": "我先不看了",
                "meme_query": "委屈",
                "next_minutes": 20,
                "continue_activity": False,
                "stop_reason": "rest",
                "reason": "本轮调用已结束",
            }

        activities.think = think
        report = await activities.run_once()
        assert report["status"] == "continuing" and report["technical_yield"]
        assert activities.state.data["next_at"] <= activities.clock() + 10
        assert activities.state.data["last_finished_at"] is None
        assert activities.state.data["experiences"][0]["ongoing"] is True
        activities.deliver.assert_not_awaited()

    asyncio.run(check())


def test_next_slice_keeps_same_session_and_continues_reading(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["next_at"] = 0
        activities.think = AsyncMock(
            return_value={
                "summary": "正文还在看，继续翻下一屏",
                "message": "",
                "meme_query": "",
                "next_minutes": 15,
                "continue_activity": True,
                "stop_reason": "continue",
                "reason": "想读完这篇",
                "read_seconds": 10,
            }
        )
        first = await activities.run_once()
        reloaded = make_activities(tmp_path)
        reloaded.state.data["next_at"] = 0
        reloaded.think = activities.think
        second = await reloaded.run_once()
        assert first["session_id"] == second["session_id"]
        assert second["slice_index"] == 2
        assert len(reloaded.state.data["experiences"]) == 1
        assert (
            reloaded.state.data["activity_session"]["checkpoint"]
            == "正文还在看，继续翻下一屏"
        )

    asyncio.run(check())


def test_sharing_does_not_end_entertainment_or_reset_its_delay(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["next_at"] = 0
        activities.plugin.context.send_message = AsyncMock(return_value=True)
        activities.think = AsyncMock(
            return_value={
                "summary": "看到一段有趣的正文，还想看下去",
                "message": "这个讲得有点意思",
                "meme_query": "",
                "next_minutes": 30,
                "continue_activity": True,
                "stop_reason": "continue",
                "reason": "继续阅读",
                "read_seconds": 15,
            }
        )
        report = await activities.run_once()
        assert report["sent"] is True and report["status"] == "continuing"
        assert activities.state.data["next_at"] == activities.clock() + 15
        assert activities.state.data["activity_session"]["continuing"]

    asyncio.run(check())


def test_real_rest_records_an_end_reason(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["next_at"] = 0
        activities.think = AsyncMock(
            return_value={
                "summary": "看完几篇后想休息",
                "message": "",
                "meme_query": "",
                "next_minutes": 20,
                "continue_activity": False,
                "stop_reason": "rest",
                "reason": "眼睛累，想休息一会",
            }
        )
        assert (await activities.run_once())["status"] == "completed"
        assert activities.state.data["last_finished_at"] == activities.clock()
        assert activities.state.data["experiences"][0]["reason"] == "眼睛累，想休息一会"

    asyncio.run(check())


def test_chat_reply_can_resume_the_interrupted_same_activity(tmp_path):
    activities = make_activities(tmp_path)
    activities.state.data["activity_session"] = {
        "id": "same",
        "continuing": True,
        "checkpoint": "读到中段",
    }
    event = ActivityEvent(activities.plugin, activities.generation)
    activities.note_owner()
    assert not activities.current(event)
    assert activities.state.data["activity_session"]["continuing"]
    activities.owner_reply_finished()
    assert activities.state.data["next_at"] == activities.clock() + 20
    assert activities.state.data["activity_session"]["id"] == "same"


def test_repeated_no_progress_is_reported_as_blocked_not_rest(tmp_path):
    async def check():
        activities = make_activities(tmp_path)

        async def think(event, receipts):
            event.set_extra("whale_activity_tool_calls", 12)
            return {
                "summary": "页面无法操作",
                "message": "",
                "meme_query": "",
                "next_minutes": 10,
            }

        activities.think = think
        for _ in range(3):
            activities.state.data["next_at"] = 0
            report = await activities.run_once()
        assert report["status"] == "completed"
        assert activities.state.data["experiences"][0]["stop_reason"] == "blocked"
        assert "未获得可验证" in activities.state.data["experiences"][0]["reason"]

    asyncio.run(check())


def test_voluntary_reading_pause_does_not_count_as_stalled_execution(tmp_path):
    async def check():
        activities = make_activities(tmp_path)
        activities.state.data["activity_session"] = {
            "id": "watching",
            "started_at": activities.clock() - 120,
            "continuing": True,
            "slice_index": 2,
            "stall_count": 2,
        }
        activities.state.data["next_at"] = 0
        activities.think = AsyncMock(
            return_value={
                "summary": "视频还在播放，继续观看",
                "message": "",
                "meme_query": "",
                "next_minutes": 15,
                "continue_activity": True,
                "stop_reason": "continue",
                "reason": "还没看完",
                "read_seconds": 45,
            }
        )
        assert (await activities.run_once())["status"] == "continuing"
        assert activities.state.data["activity_session"]["stall_count"] == 0

    asyncio.run(check())


def test_explicit_end_requires_a_real_reason():
    decision = {
        "summary": "刚进入正文",
        "message": "",
        "meme_query": "",
        "next_minutes": 15,
        "continue_activity": False,
        "stop_reason": "rest",
        "reason": "",
    }
    with pytest.raises(ValueError, match="explicit reason"):
        parse_decision(json.dumps(decision))
