import asyncio
import json
from types import SimpleNamespace

import main as plugin_module
from astrbot.core.agent.tool import FunctionTool, ToolSet
from main import WhaleDevicePlugin


class Event:
    def __init__(self, sender="12345", private=True, platform="aiocqhttp", group=""):
        self.sender, self.private, self.platform, self.group = (
            sender,
            private,
            platform,
            group,
        )
        self.extras = {}

    def get_sender_id(self):
        return self.sender

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return "whale_qq" if self.platform == "aiocqhttp" else self.platform

    def is_private_chat(self):
        return self.private

    def get_group_id(self):
        return self.group

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class Plugin:
    owner_user_id = "12345"
    platform_id = "whale_qq"
    owner_persona_id = "whale_owner"
    group_persona_id = "whale_group"
    context = SimpleNamespace(persona_manager=None)
    _is_owner_private = WhaleDevicePlugin._is_owner_private
    _relation = WhaleDevicePlugin._relation
    _persona_id = WhaleDevicePlugin._persona_id
    _persona_prompt = WhaleDevicePlugin._persona_prompt
    _replace_persona_block = staticmethod(WhaleDevicePlugin._replace_persona_block)
    _can_use_own_phone = WhaleDevicePlugin._can_use_own_phone


def test_empty_owner_notifications_do_not_interrupt_entertainment():
    from unittest.mock import Mock

    plugin = Plugin()
    plugin.activities = SimpleNamespace(note_owner=Mock(), pause=Mock())
    event = Event()
    event.get_message_str = lambda: ""
    event.message_obj = SimpleNamespace(message=[])
    asyncio.run(WhaleDevicePlugin.note_owner_presence(plugin, event))
    plugin.activities.note_owner.assert_not_called()


def test_real_owner_reply_releases_existing_entertainment():
    from unittest.mock import Mock

    plugin = Plugin()
    plugin.activities = SimpleNamespace(owner_reply_finished=Mock())
    asyncio.run(WhaleDevicePlugin.resume_personal_activity(plugin, Event()))
    plugin.activities.owner_reply_finished.assert_called_once()


def test_owner_tool_access_is_exact_private_only():
    plugin = Plugin()
    assert plugin._is_owner_private(Event())
    assert not plugin._is_owner_private(Event(private=False, group="44"))
    assert not plugin._is_owner_private(Event(sender="012345"))
    assert not plugin._is_owner_private(Event(platform="discord"))
    assert plugin._relation(Event(private=False, group="44")) == "group"
    assert plugin._relation(Event(sender="8")) == "guest_private"


def test_llm_hook_keeps_own_phone_tool_for_group():
    plugin = Plugin()
    plugin.restrict_device_tool_and_relation = (
        WhaleDevicePlugin.restrict_device_tool_and_relation.__get__(plugin)
    )
    phone = FunctionTool(
        name="whale_device_execute_plan",
        description="phone",
        parameters={"type": "object", "properties": {}},
    )
    other = FunctionTool(
        name="other",
        description="other",
        parameters={"type": "object", "properties": {}},
    )
    request = SimpleNamespace(
        func_tool=ToolSet([phone, other]), extra_user_content_parts=[], system_prompt=""
    )
    asyncio.run(
        plugin.restrict_device_tool_and_relation(
            Event(private=False, group="44"), request
        )
    )
    assert [item.name for item in request.func_tool.tools] == [
        "whale_device_execute_plan",
        "other",
    ]
    assert "群" in request.extra_user_content_parts[0].text
    assert "whale_phone_instructions" in request.extra_user_content_parts[0].text
    assert "snapshot_id" not in request.extra_user_content_parts[0].text


class PersonaManager:
    async def get_persona(self, persona_id):
        return SimpleNamespace(system_prompt=f"PROMPT:{persona_id}")


class RoutedPlugin(Plugin):
    context = SimpleNamespace(persona_manager=PersonaManager())


def test_persona_routing_owner_guest_and_unknown_platform():
    plugin = RoutedPlugin()
    assert plugin._persona_id(Event(private=False, group="44")) == "whale_owner"
    assert plugin._persona_id(Event()) == "whale_owner"
    assert (
        plugin._persona_id(Event(sender="8", private=False, group="44"))
        == "whale_group"
    )
    assert plugin._persona_id(Event(sender="8")) == "whale_group"
    assert plugin._persona_id(Event(platform="discord")) == "whale_group"


def test_llm_hook_persona_is_request_local_and_own_phone_is_available_in_groups():
    plugin = RoutedPlugin()
    plugin.restrict_device_tool_and_relation = (
        WhaleDevicePlugin.restrict_device_tool_and_relation.__get__(plugin)
    )

    async def invoke(event):
        phone = FunctionTool(
            name="whale_device_execute_plan",
            description="phone",
            parameters={"type": "object", "properties": {}},
        )
        request = SimpleNamespace(
            func_tool=ToolSet([phone]),
            extra_user_content_parts=[],
            system_prompt="SAFE\n<persona_instructions>old</persona_instructions>\nTAIL",
        )
        await plugin.restrict_device_tool_and_relation(event, request)
        return request, event.extras

    async def run():
        owner_group = Event(private=False, group="44")
        guest_group = Event(sender="8", private=False, group="44")
        (owner_req, owner_extras), (guest_req, guest_extras) = await asyncio.gather(
            invoke(owner_group), invoke(guest_group)
        )
        owner_private_req, _ = await invoke(Event())
        return owner_req, guest_req, owner_private_req, owner_extras, guest_extras

    owner_req, guest_req, owner_private_req, owner_extras, guest_extras = asyncio.run(
        run()
    )
    assert "PROMPT:whale_owner" in owner_req.system_prompt
    assert "PROMPT:whale_group" in guest_req.system_prompt
    assert "SAFE" in guest_req.system_prompt and "TAIL" in guest_req.system_prompt
    assert "PROMPT:whale_owner" in owner_private_req.system_prompt
    assert owner_extras["whale_persona_id"] == "whale_owner"
    assert guest_extras["whale_persona_id"] == "whale_group"
    assert owner_req.func_tool.tools and guest_req.func_tool.tools
    assert [tool.name for tool in owner_private_req.func_tool.tools] == [
        "whale_device_execute_plan"
    ]


def test_selected_persona_replaces_initial_dialogues_without_changing_history():
    plugin = RoutedPlugin()

    class Manager:
        async def get_persona(self, persona_id):
            return SimpleNamespace(
                system_prompt=persona_id, begin_dialogs=["你好", persona_id + "示例"]
            )

    plugin.context = SimpleNamespace(persona_manager=Manager())
    history = {"role": "user", "content": "真实历史"}
    req = SimpleNamespace(
        func_tool=None,
        extra_user_content_parts=[],
        system_prompt="<persona_instructions>old</persona_instructions>",
        contexts=[
            {"role": "user", "content": "旧示例", "_no_save": True},
            {"role": "assistant", "content": "旧回复", "_no_save": True},
            history,
        ],
    )
    asyncio.run(
        WhaleDevicePlugin.restrict_device_tool_and_relation(plugin, Event(), req)
    )
    assert [item["content"] for item in req.contexts] == [
        "你好",
        "whale_owner示例",
        "真实历史",
    ]
    assert req.contexts[-1] is history


def test_two_tool_calls_in_one_qq_message_use_distinct_execution_ids(monkeypatch):
    plugin = RoutedPlugin()
    plugin._headless_service = None
    plugin.config = {"device_id": "phone"}
    plugin.device_id = "phone"
    plugin.token_env = "WHALE_TEST_TOOL_TOKEN"
    monkeypatch.setenv(plugin.token_env, "t" * 48)
    calls = []
    plugin._rpc = lambda body, token: calls.append(json.loads(body)) or "actual result"
    event = Event()
    event.message_obj = SimpleNamespace(message_id="qq-message-1")

    async def invoke():
        await WhaleDevicePlugin.execute_plan(
            plugin, event, [{"command": "device.device_status", "payload": {}}]
        )
        await WhaleDevicePlugin.execute_plan(
            plugin,
            event,
            [
                {
                    "command": "device.media.pick",
                    "payload": {"media_type": "image", "max_items": 1},
                }
            ],
        )

    asyncio.run(invoke())
    assert len(calls) == 2 and calls[0]["message_id"] != calls[1]["message_id"]
    assert all(len(call["message_id"]) <= 120 for call in calls)
    assert calls[0]["commands"][0]["command"] == "device.device_status"
    assert calls[1]["commands"][0]["command"] == "device.media.pick"


def test_rpc_result_names_actual_command_and_preserves_failure_evidence(monkeypatch):
    plugin = RoutedPlugin()
    plugin.rpc_url = "http://127.0.0.1:8767/v1/device/execute-plan"
    result = {
        "kind": "failed",
        "reason": "NOT_WIRED",
        "results": [
            {
                "status": "denied",
                "error_code": "NOT_WIRED",
                "message": "foreground required",
            }
        ],
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps({"result": result}).encode()

    monkeypatch.setattr(
        plugin_module.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kw: Response()),
    )
    commands = [
        {
            "command": "device.media.pick",
            "payload": {"media_type": "image", "max_items": 1},
        }
    ]
    response = json.loads(
        WhaleDevicePlugin._rpc(
            plugin, json.dumps({"commands": commands}).encode(), "t" * 48
        )
    )
    assert response["command_results"][0]["command"] == "device.media.pick"
    assert response["device_result"] == result
    assert (
        "NOT_WIRED" in response["instruction"]
        and "不等于手机离线" in response["instruction"]
    )


def test_rpc_rejects_success_without_command_receipts(monkeypatch):
    plugin = RoutedPlugin()
    plugin.rpc_url = "http://127.0.0.1:8767/v1/device/execute-plan"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"result":{"kind":"device_task","results":[]}}'

    monkeypatch.setattr(
        plugin_module.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kw: Response()),
    )
    response = WhaleDevicePlugin._rpc(
        plugin,
        json.dumps(
            {"commands": [{"command": "device.device_status", "payload": {}}]}
        ).encode(),
        "t" * 48,
    )
    assert "未完成" in response and "回执不完整" in response


def test_persona_selection_happens_before_prompt_construction():
    plugin = RoutedPlugin()
    owner, group = Event(), Event(sender="8", private=False, group="44")

    async def select():
        await WhaleDevicePlugin.select_speaker_persona(plugin, owner)
        await WhaleDevicePlugin.select_speaker_persona(plugin, group)

    asyncio.run(select())
    assert owner.get_extra("_request_persona_id") == "whale_owner"
    assert group.get_extra("_request_persona_id") == "whale_group"


def test_gallery_read_returns_private_model_image_and_can_send_to_current_group(
    monkeypatch,
):
    import base64
    from unittest.mock import AsyncMock

    from mcp.types import CallToolResult

    plugin = RoutedPlugin()
    plugin._headless_service = None
    plugin.config = {"device_id": "phone"}
    plugin.device_id = "phone"
    plugin.token_env = "WHALE_TEST_TOOL_TOKEN"
    monkeypatch.setenv(plugin.token_env, "t" * 48)
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    image_data = base64.b64encode(b"bounded-image-bytes").decode()
    result = {
        "status": "ok",
        "evidence": {
            "media_id": "123",
            "mime": "image/jpeg",
            "image_base64": image_data,
        },
    }
    plugin._rpc = lambda *args: json.dumps(
        {
            "device_result": {"results": [result]},
            "command_results": [{"command": "device.gallery.read", "result": result}],
        }
    )
    event = Event(sender="8", private=False, group="44")
    event.message_obj = SimpleNamespace(message_id="group-photo")
    event.unified_msg_origin = "whale_qq:GroupMessage:44"

    async def run():
        viewed = await WhaleDevicePlugin.execute_plan(
            plugin,
            event,
            [{"command": "device.gallery.read", "payload": {"media_id": "123"}}],
        )
        assert isinstance(viewed, CallToolResult)
        assert viewed.content[1].type == "image"
        assert image_data not in viewed.content[0].text
        plugin.context.send_message.assert_not_awaited()
        assert json.loads(await WhaleDevicePlugin.send_photo(plugin, event, "123"))[
            "sent"
        ]
        assert json.loads(await WhaleDevicePlugin.send_photo(plugin, event, "123"))[
            "already_sent"
        ]

    asyncio.run(run())
    plugin.context.send_message.assert_awaited_once()
    assert plugin.context.send_message.call_args.args[0] == "whale_qq:GroupMessage:44"


def test_photo_delivery_requires_photo_viewed_in_current_event():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    plugin.context = SimpleNamespace(send_message=AsyncMock())
    event = Event(private=False, group="44")
    result = json.loads(
        asyncio.run(WhaleDevicePlugin.send_photo(plugin, event, "not-viewed"))
    )
    assert result == {"sent": False, "reason": "view_photo_first"}
    plugin.context.send_message.assert_not_awaited()


def test_owner_followup_rereads_recent_photo_and_delivers_before_reply():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    event = Event()
    event.unified_msg_origin = "whale_qq:FriendMessage:12345"
    plugin._recent_photo_ids = {event.unified_msg_origin: ["2182"]}
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))

    async def read(event, commands):
        assert commands == [
            {"command": "device.gallery.read", "payload": {"media_id": "2182"}}
        ]
        event.set_extra("whale_phone_photos", {"2182": {"data": "aW1hZ2U="}})

    plugin.execute_plan = AsyncMock(side_effect=read)
    result = json.loads(
        asyncio.run(WhaleDevicePlugin.send_photo(plugin, event, "last"))
    )
    assert result["sent"] and result["photo_id"] == "2182"
    plugin.execute_plan.assert_awaited_once()
    plugin.context.send_message.assert_awaited_once()


def test_group_cannot_reuse_owner_recent_photo_without_viewing():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    event = Event(sender="8", private=False, group="44")
    event.unified_msg_origin = "whale_qq:GroupMessage:44"
    plugin._recent_photo_ids = {"whale_qq:FriendMessage:12345": ["2182"]}
    plugin.execute_plan = AsyncMock()
    plugin.context = SimpleNamespace(send_message=AsyncMock())
    result = json.loads(
        asyncio.run(WhaleDevicePlugin.send_photo(plugin, event, "last"))
    )
    assert not result["sent"]
    plugin.execute_plan.assert_not_awaited()
    plugin.context.send_message.assert_not_awaited()


def test_failed_owner_photo_reread_does_not_claim_delivery():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    plugin.execute_plan = AsyncMock(return_value="Read failed")
    plugin.context = SimpleNamespace(send_message=AsyncMock())
    result = json.loads(
        asyncio.run(WhaleDevicePlugin.send_photo(plugin, Event(), "2182"))
    )
    assert not result["sent"]
    plugin.context.send_message.assert_not_awaited()


def test_show_photo_uses_one_based_position_and_sends_owner_picture():
    from unittest.mock import AsyncMock

    from mcp.types import CallToolResult, TextContent

    plugin = RoutedPlugin()
    plugin.execute_plan = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "command_results": [
                        {
                            "result": {
                                "status": "ok",
                                "artifacts": [{"media_id": "2182"}],
                            }
                        }
                    ]
                }
            ),
            CallToolResult(
                content=[TextContent(type="text", text="actual photo returned")]
            ),
        ]
    )
    plugin.send_photo = AsyncMock(return_value=json.dumps({"sent": True}))
    event = Event()
    result = asyncio.run(WhaleDevicePlugin.show_photo(plugin, event, 40))
    assert plugin.execute_plan.call_args_list[0].args[1] == [
        {"command": "device.gallery.list", "payload": {"limit": 1, "offset": 39}}
    ]
    assert plugin.execute_plan.call_args_list[1].args[1] == [
        {"command": "device.gallery.read", "payload": {"media_id": "2182"}}
    ]
    plugin.send_photo.assert_awaited_once_with(event, "2182")
    assert json.loads(result.content[0].text)["delivery"]["sent"]


def test_show_photo_leaves_group_friend_sharing_to_character():
    from unittest.mock import AsyncMock

    from mcp.types import CallToolResult, TextContent

    plugin = RoutedPlugin()
    plugin.execute_plan = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "command_results": [
                        {
                            "result": {
                                "status": "ok",
                                "artifacts": [{"media_id": "2182"}],
                            }
                        }
                    ]
                }
            ),
            CallToolResult(
                content=[TextContent(type="text", text="actual photo returned")]
            ),
        ]
    )
    plugin.send_photo = AsyncMock()
    event = Event(sender="8", private=False, group="44")
    result = asyncio.run(WhaleDevicePlugin.show_photo(plugin, event, 40))
    plugin.send_photo.assert_not_awaited()
    assert (
        json.loads(result.content[0].text)["delivery"]["decision"]
        == "review_image_then_decide"
    )


def test_owner_can_resend_recent_screenshot_only_within_same_conversation():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    plugin.context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin.execute_plan = AsyncMock()
    event = Event()
    event.unified_msg_origin = "whale_qq:FriendMessage:12345"
    plugin._recent_photo_ids = {event.unified_msg_origin: ["screen:one"]}
    plugin._recent_screen_images = {
        event.unified_msg_origin: {"screen:one": {"data": "aW1hZ2U="}}
    }
    delivered = json.loads(
        asyncio.run(WhaleDevicePlugin.send_photo(plugin, event, "last"))
    )
    assert delivered["sent"] and delivered["photo_id"] == "screen:one"
    other_event = Event(private=False, group="44")
    other_event.unified_msg_origin = "whale_qq:GroupMessage:44"
    denied = json.loads(
        asyncio.run(WhaleDevicePlugin.send_photo(plugin, other_event, "screen:one"))
    )
    assert not denied["sent"]
    plugin.execute_plan.assert_not_awaited()
    plugin.context.send_message.assert_awaited_once()


def test_camera_tool_delivers_actual_current_photo_to_owner():
    import base64
    from unittest.mock import AsyncMock

    from mcp.types import CallToolResult, ImageContent, TextContent

    plugin = RoutedPlugin()
    event = Event()
    event.set_extra("whale_phone_photos", {"camera-one": {"data": "aW1hZ2U="}})
    plugin.execute_plan = AsyncMock(
        return_value=CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "command_results": [
                                {
                                    "command": "device.camera.capture",
                                    "result": {
                                        "status": "ok",
                                        "evidence": {"photo_id": "camera-one"},
                                    },
                                }
                            ]
                        }
                    ),
                ),
                ImageContent(
                    type="image",
                    data=base64.b64encode(b"photo").decode(),
                    mimeType="image/jpeg",
                ),
            ]
        )
    )
    plugin.send_photo = AsyncMock(return_value=json.dumps({"sent": True}))
    result = asyncio.run(WhaleDevicePlugin.take_photo(plugin, event, "front"))
    plugin.execute_plan.assert_awaited_once_with(
        event,
        [
            {
                "command": "device.camera.capture",
                "payload": {"lens": "front", "quality": "standard"},
            }
        ],
    )
    plugin.send_photo.assert_awaited_once_with(event, "camera-one")
    assert json.loads(result.content[0].text)["delivery"]["sent"]


def test_rpc_distinguishes_missing_phone_connection_from_missing_feature(monkeypatch):
    plugin = RoutedPlugin()
    plugin.rpc_url = "http://127.0.0.1:8767/v1/device/execute-plan"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(
                {
                    "result": {
                        "kind": "failed",
                        "results": [
                            {
                                "status": "failed",
                                "error_code": "NOT_WIRED",
                                "message": "no device connection is available",
                            }
                        ],
                    }
                }
            ).encode()

    monkeypatch.setattr(
        plugin_module.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kw: Response()),
    )
    result = json.loads(
        WhaleDevicePlugin._rpc(
            plugin,
            json.dumps(
                {
                    "sender_id": "12345",
                    "commands": [{"command": "device.screen.capture", "payload": {}}],
                }
            ).encode(),
            "t" * 48,
        )
    )
    assert "连接不存在或已断开" in result["instruction"]
    assert "不能说没有截屏功能" in result["instruction"]


def test_screenshot_only_captures_confirmed_current_page_without_navigation_or_send():
    from unittest.mock import AsyncMock

    from mcp.types import CallToolResult, ImageContent, TextContent

    plugin = RoutedPlugin()
    event = Event()
    calls = []
    evidence = {
        "package_name": "com.huawei.android.launcher",
        "window_id": "1",
        "nodes": "second page apps",
        "current_page": "2",
        "total_pages": "3",
    }
    event.set_extra("whale_screen_confirmation", evidence)

    async def execute(event, commands):
        command = commands[0]["command"]
        calls.append(command)
        if command == "device.screen.capture":
            event.set_extra(
                "whale_phone_photos",
                {"screen:two": {"data": "aW1hZ2U=", "source": command}},
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "command_results": [
                                    {
                                        "command": command,
                                        "result": {
                                            "status": "ok",
                                            "evidence": {
                                                "photo_id": "screen:two",
                                                "package": "com.huawei.android.launcher",
                                            },
                                        },
                                    }
                                ]
                            }
                        ),
                    ),
                    ImageContent(type="image", data="aW1hZ2U=", mimeType="image/jpeg"),
                ]
            )
        return json.dumps(
            {"command_results": [{"result": {"status": "ok", "evidence": evidence}}]}
        )

    plugin.execute_plan = execute
    plugin.send_photo = AsyncMock()
    result = asyncio.run(WhaleDevicePlugin.capture_screen(plugin, event))
    assert calls == ["device.screen.capture", "device.ui.observe"]
    receipt = json.loads(result.content[0].text)
    assert receipt["current_page"] == "2" and not receipt["delivery"]["sent"]
    assert event.get_extra("whale_phone_photos")["screen:two"]["verified_screenshot"]
    plugin.send_photo.assert_not_awaited()


def test_screenshot_requires_command_confirmation_first():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    plugin.execute_plan = AsyncMock()
    plugin.send_photo = AsyncMock()
    result = asyncio.run(WhaleDevicePlugin.capture_screen(plugin, Event()))
    assert json.loads(result)["reason"] == "observe_current_page_first"
    plugin.execute_plan.assert_not_awaited()
    plugin.send_photo.assert_not_awaited()


def test_screenshot_waits_until_command_reports_scroll_is_settled():
    from unittest.mock import AsyncMock

    plugin = RoutedPlugin()
    plugin.execute_plan = AsyncMock()
    event = Event()
    event.set_extra(
        "whale_screen_confirmation", {"current_page": "2", "page_settled": "false"}
    )
    result = asyncio.run(WhaleDevicePlugin.capture_screen(plugin, event))
    assert json.loads(result)["reason"] == "page_still_scrolling"
    plugin.execute_plan.assert_not_awaited()


def test_navigation_invalidates_previous_screen_confirmation():

    plugin = RoutedPlugin()
    event = Event()
    event.set_extra("whale_screen_confirmation", {"package_name": "old"})
    plugin._headless_service = None
    plugin.config = {}
    plugin.device_id = ""
    asyncio.run(
        WhaleDevicePlugin.execute_plan(
            plugin, event, [{"command": "device.ui.home", "payload": {}}]
        )
    )
    assert event.get_extra("whale_screen_confirmation") is None


def test_group_reply_is_owned_by_astrbot_and_phone_instructions_are_lazy():
    assert not hasattr(WhaleDevicePlugin, "configure_group_reply")
    plugin = Plugin()
    event = Event()
    result = asyncio.run(WhaleDevicePlugin.phone_instructions(plugin, event))
    assert "snapshot_id" in result
    assert "device.ui.observe" in result
    assert "unavailable" in asyncio.run(
        WhaleDevicePlugin.phone_instructions(plugin, Event(sender="8"))
    )
