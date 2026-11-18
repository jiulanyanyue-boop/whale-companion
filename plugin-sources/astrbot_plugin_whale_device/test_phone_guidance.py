import asyncio
import json

import jsonschema
import pytest
from astrbot_whale_device.astrbot_native_bridge import GROUP_COMMANDS
from astrbot_whale_device.validation import SHAPES, validate_device_plan
from phone_guidance import phone_guide
from test_main import Event, Plugin

from astrbot.core.provider.register import llm_tools
from main import WhaleDevicePlugin


def test_ui_example_uses_current_observed_ids_and_passes_command_validation():
    evidence = {
        "snapshot_id": "fresh-snapshot",
        "package_name": "social.app",
        "nodes": "actual-target|actual-hash|ScrollView|阅读内容||0,0,100,400|false|false|true",
    }
    guide = phone_guide("ui", set(SHAPES), evidence)
    example_line = next(line for line in guide.splitlines() if "滚动格式示例" in line)
    example = json.loads(example_line[example_line.index('{"commands"') :])
    validate_device_plan(example["commands"], None)
    assert example["commands"][0]["payload"]["snapshot_id"] == "fresh-snapshot"
    assert example["commands"][0]["payload"]["target_id"] == "actual-target"
    assert example["commands"][0]["payload"]["selector_hash"] == "actual-hash"
    assert "duration_ms" in guide
    assert "device.sms.send" not in guide


def test_help_without_observation_does_not_fabricate_ui_ids():
    guide = phone_guide("ui", set(SHAPES))
    assert '"target_id":' not in guide
    example = next(line for line in guide.splitlines() if line.startswith("先观察："))
    validate_device_plan(json.loads(example.split("：", 1)[1])["commands"], None)


def test_help_scopes_commands_and_rejects_unknown_topics():
    guide = phone_guide("ui", GROUP_COMMANDS)
    assert "device.ui.tap:" not in guide and "device.ui.input:" not in guide
    assert "no permitted" in phone_guide("camera", GROUP_COMMANDS)
    assert "device.system.unlock:" not in phone_guide("system", GROUP_COMMANDS)
    assert "Unknown topic" in phone_guide("not-a-topic", set(SHAPES))


def test_photo_help_distinguishes_view_and_delivery():
    guide = phone_guide("gallery", set(SHAPES))
    assert "序号" in guide and "media_id" in guide
    assert "read 只看图" in guide and "sent=true" in guide
    assert "device.ui.tap:" not in guide


def test_group_phone_help_has_no_owner_only_commands():
    guide = asyncio.run(
        WhaleDevicePlugin.phone_instructions(
            Plugin(), Event(private=False, group="44"), "system"
        )
    )
    assert "device.device_status:" in guide
    assert "device.sms.send:" not in guide
    assert "device.system.unlock:" not in guide


def test_registered_tool_schemas_express_bounds_and_optional_defaults():
    schema = llm_tools.get_func("whale_phone_show_photo").parameters
    jsonschema.validate({"position": 40}, schema)
    for invalid in [0, 1.5, True, 1002]:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"position": invalid}, schema)
    jsonschema.validate({}, llm_tools.get_func("whale_phone_instructions").parameters)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"topic": "arbitrary"},
            llm_tools.get_func("whale_phone_instructions").parameters,
        )
    for name in ["whale_phone_take_photo", "whale_phone_watch_screen"]:
        jsonschema.validate({}, llm_tools.get_func(name).parameters)
    schema = llm_tools.get_func("whale_device_execute_plan").parameters
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"commands": ["device.ui.home"]}, schema)


@pytest.mark.parametrize(
    "event",
    [
        Event(),
        Event(sender="8"),
        Event(private=False, group="44"),
        Event(sender="8", private=False, group="44"),
    ],
)
def test_phone_role_is_present_without_loading_phone_tools(event):
    from types import SimpleNamespace

    request = SimpleNamespace(
        func_tool=None, contexts=[], extra_user_content_parts=[], system_prompt=""
    )
    asyncio.run(
        WhaleDevicePlugin.restrict_device_tool_and_relation(Plugin(), event, request)
    )
    assert "<whale_phone_role>" in request.system_prompt
    assert "属于小鲸鱼本人" in request.system_prompt
    assert "真实工具回执" in request.system_prompt
    assert "snapshot_id" not in request.system_prompt
    assert not hasattr(Plugin, "phone_instructions")
