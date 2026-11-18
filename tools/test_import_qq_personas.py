import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "qq_personas", Path(__file__).with_name("import-qq-personas.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_multiline_examples_and_alternatives_exclude_next_section():
    text = """===== FILE: role/prompt.txt =====
角色原文
## 七、回复示例（空格表示分条）
群友：你好
你可以：在呢 干嘛
或者：刚睡醒
## 八、其他要求
不要导入这段
===== FILE: role/persona.json =====
{"title":"metadata"}
"""
    assert module.extract_begin_dialogs(text) == [
        "你好",
        "在呢\n\n干嘛",
        "你好",
        "刚睡醒",
    ]
    assert module.extract_prompt(text).startswith("角色原文")
    assert "metadata" not in module.extract_prompt(text)


def test_inline_examples_keep_sequential_replies_in_one_assistant_turn():
    text = """十一、回复示例（空格表示分成两条消息）
群友：上号 你可以：来了来了 等我两分钟 或者：今天不想打
群友：查查版本 你可以：我看看啊 然后：查完了
十二、群文化自适应
其他内容
"""
    assert module.extract_begin_dialogs(text) == [
        "上号",
        "来了来了\n\n等我两分钟",
        "上号",
        "今天不想打",
        "查查版本",
        "我看看啊\n\n查完了",
    ]


def test_plain_persona_without_examples_has_no_preset_dialogue():
    assert module.extract_begin_dialogs("你是小鲸鱼。") == []


def test_owner_identity_executes_clear_image_requests_without_reconfirmation():
    prompt = module.apply_phone_identity("你是小鲸鱼。", "whale_owner")
    assert module.apply_phone_identity(prompt, "whale_owner") == prompt
    assert prompt.count("<whale_own_phone_identity>") == 1
    assert "主人明确要求照片时按主人专属人设直接处理" in prompt
    assert "在群里可以自行决定发不发" not in prompt


def test_group_identity_keeps_independent_photo_judgment():
    prompt = module.apply_phone_identity("你是小鲸鱼。", "whale_group")
    assert "在群里可以自行决定发不发自己的照片" in prompt
    assert "主人明确要求发图时直接看图并发到当前对话" not in prompt
