"""一键启动（`host/run_host.py` + `quickstart.py`）的固定测试。

## 这些测试回答什么问题

不是「函数返回对不对」，而是三条**使用者会真实撞上**的场景：

1. **缺密钥时**：报告里必须出现 `WHALE_SIGNING_SECRET` 这个名字 + 中文怎么补，
   而且**绝不能出现密钥的值**（哪怕一个字符）；
2. **首次运行**：没有配置就生成一份，第二次跑**不覆盖**使用者改过的内容；
3. **配置写坏时**：`HostConfig` 的英文异常要被翻成中文可照做的提示，
   而不是把 traceback 甩给使用者。

## 为什么「不泄漏」要单独占一条测试

密钥泄漏是最难靠 code review 兜住的一类缺陷：只要有人为了「方便调试」把
`environ[name]` 拼进消息，就会一路印到终端和日志里。所以这里不检查代码意图，
而是**真的构造一个带值的环境，然后在返回值 repr 与渲染文本里逐字搜索那个值**。
假密钥里故意混入 `SIGNING` / `MODEL` 之类的字样，防止「按名字过滤」的假修复。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

import run_host
from astrbot_whale_device import quickstart
from astrbot_whale_device.host_config import ConfigError, HostConfig
from astrbot_whale_device.quickstart import (
    DEFAULT_TEMPLATE,
    MIN_PYTHON,
    CheckItem,
    check_environment,
    default_template,
    ensure_config_file,
    explain_config_error,
    format_report,
    format_success,
    load_config,
    validate_config_file,
)

# 假的密钥值：混入 SIGNING / MODEL / NAPCAT 字样，防止实现「按值内容过滤」的假修复。
FAKE_SIGNING_SECRET = "SIGNING-do-not-print-4b49b0a1"
FAKE_MODEL_KEY = "MODEL-do-not-print-c0ffee12"
FAKE_ONEBOT_TOKEN = "NAPCAT-do-not-print-9e2f77"

SECRET_ENV = "WHALE_SIGNING_SECRET_UT"
MODEL_ENV = "WHALE_MODEL_KEY_UT"
ONEBOT_ENV = "NAPCAT_TOKEN_UT"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _valid_config_mapping(**overrides) -> dict:
    """一份**能通过 HostConfig 全部校验**的配置（模板 + 可覆盖字段）。"""
    raw = json.loads(json.dumps(DEFAULT_TEMPLATE))
    raw["signing_secret_env"] = SECRET_ENV
    raw["model_api_key_env"] = MODEL_ENV
    raw["onebot"]["access_token_env"] = ONEBOT_ENV
    raw.update(overrides)
    return raw


def _write_config(tmp_path, mapping) -> str:
    path = tmp_path / "host.json"
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _full_environ() -> dict[str, str]:
    return {
        SECRET_ENV: FAKE_SIGNING_SECRET,
        MODEL_ENV: FAKE_MODEL_KEY,
        ONEBOT_ENV: FAKE_ONEBOT_TOKEN,
    }


# ---------------------------------------------------------------------------
# 内置模板必须自洽：生成出来的配置不能被自己的校验打回
# ---------------------------------------------------------------------------


def test_default_template_loads_through_host_config():
    """内置模板必须能被 `HostConfig` 接受——否则「生成配置」等于生成一堆垃圾。"""
    config = HostConfig.from_mapping(DEFAULT_TEMPLATE)
    assert config.owner_user_id == "10001"
    assert config.signing_secret_env == "WHALE_SIGNING_SECRET"
    assert config.model_api_key_env == "WHALE_MODEL_KEY"
    assert config.onebot is not None
    # 模板里不能有 `_comment` / `_usage` 这类注解键：HostConfig 拒绝未知键，
    # 抄 examples/host.example.json 时最容易踩的坑就在这里。
    assert all(not str(key).startswith("_") for key in DEFAULT_TEMPLATE)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_host_config_accepts_windows_utf8_bom_without_rewriting(tmp_path, encoding):
    """Windows 编辑器或 PowerShell 写入 BOM 后仍能读取，且不改动原配置。"""
    path = tmp_path / "中文 空格" / "host.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_valid_config_mapping()), encoding=encoding)
    original = path.read_bytes()
    assert HostConfig.load(path).owner_user_id == DEFAULT_TEMPLATE["owner_user_id"]
    assert path.read_bytes() == original


def test_default_template_is_a_copy_not_the_constant():
    """`default_template()` 必须给深拷贝，调用方改它不能污染后续生成。"""
    first = default_template()
    first["owner_user_id"] = "99999"
    first["model"]["model_id"] = "mutated"
    assert default_template()["owner_user_id"] == "10001"
    assert default_template()["model"]["model_id"] == DEFAULT_TEMPLATE["model"]["model_id"]


# ---------------------------------------------------------------------------
# check_environment：缺什么就说缺什么
# ---------------------------------------------------------------------------


def test_missing_signing_secret_is_reported_by_name_with_chinese_fix():
    """缺签名密钥：报告只提变量名，且 fix 是能直接照做的命令。"""
    items = check_environment(
        environ={},
        python_version=(3, 15),
        config=HostConfig.from_mapping(_valid_config_mapping()),
    )
    keys = [item.key for item in items]
    assert SECRET_ENV in keys
    signing = next(item for item in items if item.key == SECRET_ENV)
    # 三段式：key / message / fix 都要有内容
    assert signing.message
    assert signing.fix
    assert "签名" in signing.message
    assert SECRET_ENV in signing.message
    # fix 必须可直接照做（给两种 shell 的写法 + 生成随机密钥的办法）
    assert "secrets.token_hex" in signing.fix
    assert f"$env:{SECRET_ENV}" in signing.fix
    assert f"export {SECRET_ENV}=" in signing.fix


def test_missing_model_key_and_onebot_token_follow_the_config_env_names():
    """模型 Key / NapCat token 的变量名来自配置，而不是写死的常量。"""
    config = HostConfig.from_mapping(_valid_config_mapping())
    items = check_environment(environ={SECRET_ENV: FAKE_SIGNING_SECRET}, python_version=(3, 15), config=config)
    keys = {item.key for item in items}
    assert keys == {MODEL_ENV, ONEBOT_ENV}
    model_item = next(item for item in items if item.key == MODEL_ENV)
    assert "模型" in model_item.message
    onebot_item = next(item for item in items if item.key == ONEBOT_ENV)
    assert "NapCat" in onebot_item.message


def test_optional_env_names_can_be_blank_in_config():
    """配置里把 key/token 的变量名留空 → 这两项**不该**被当成缺失（本地模型场景）。"""
    raw = _valid_config_mapping()
    raw["model_api_key_env"] = ""
    raw["onebot"]["access_token_env"] = ""
    items = check_environment(
        environ={SECRET_ENV: FAKE_SIGNING_SECRET},
        python_version=(3, 15),
        config=HostConfig.from_mapping(raw),
    )
    assert items == []


def test_all_ready_returns_empty_list():
    """全部就绪时返回空列表（run_host 用它决定是否放行）。"""
    config = HostConfig.from_mapping(_valid_config_mapping())
    items = check_environment(environ=_full_environ(), python_version=(3, 15), config=config)
    assert items == []


def test_python_too_old_is_a_blocker_with_chinese_message():
    """Python 版本低于 3.11（核心包 requires-python）→ 明确说太低 + 怎么装。"""
    items = check_environment(environ=_full_environ(), python_version=(3, 10))
    python_items = [item for item in items if item.key == "python"]
    assert len(python_items) == 1
    item = python_items[0]
    assert "3.10" in item.message
    assert "3.11" in item.message
    assert "requires-python" in item.message
    assert "python.org" in item.fix
    assert item.blocking is True


def test_python_at_minimum_is_accepted():
    """恰好 3.11 必须放行——这一条防止「顺手改成 >=3.12」的回归。"""
    items = check_environment(environ=_full_environ(), python_version=(3, 11))
    assert [item for item in items if item.key == "python"] == []
    assert MIN_PYTHON == (3, 11)


@pytest.mark.parametrize("version", [(3, 15, 0), "3.15.0", sys.version_info])
def test_python_version_accepts_several_shapes(version):
    """版本参数接受 tuple / str / sys.version_info 三种形态。"""
    items = check_environment(environ=_full_environ(), python_version=version)
    assert [item for item in items if item.key == "python"] == []


def test_missing_websockets_is_reported_with_install_command():
    """缺 websockets → 提示 + 可直接照做的 pip 安装命令。"""
    # 密钥都齐，只让 websockets 缺席 —— 这样列表里只该剩它一项。
    items = check_environment(
        environ={**{DEFAULT_TEMPLATE["signing_secret_env"]: FAKE_SIGNING_SECRET}},
        python_version=(3, 15),
        websockets_available=lambda: False,
    )
    assert [item.key for item in items] == ["websockets"]
    assert "websockets" in items[0].message
    assert "pip install" in items[0].fix


# ---------------------------------------------------------------------------
# 密钥值绝不进报告
# ---------------------------------------------------------------------------


def test_report_never_contains_secret_values():
    """硬规矩：返回结构与渲染文本里都搜不到任何密钥值。"""
    config = HostConfig.from_mapping(_valid_config_mapping())
    # 环境变量里塞了真实值，但签名密钥这里故意缺失 → 逼出一条报告
    environ = {MODEL_ENV: FAKE_MODEL_KEY, ONEBOT_ENV: FAKE_ONEBOT_TOKEN}
    items = check_environment(environ=environ, python_version=(3, 15), config=config)

    blob = repr(items) + "\n" + format_report(items)
    for secret in (FAKE_MODEL_KEY, FAKE_ONEBOT_TOKEN):
        assert secret not in blob
        # 也不能出现值的片段
        assert secret.split("-do-not-print-")[1] not in blob
    # 名字必须在（否则使用者不知道缺哪个）
    assert SECRET_ENV in blob


def test_success_summary_never_contains_secret_values():
    """「全部就绪」的摘要同样只印变量名，不印值。"""
    config = HostConfig.from_mapping(_valid_config_mapping())
    text = format_success(config, "host/host.json")
    assert SECRET_ENV in text
    for secret in (FAKE_SIGNING_SECRET, FAKE_MODEL_KEY, FAKE_ONEBOT_TOKEN):
        assert secret not in text


def test_check_items_have_no_field_capable_of_holding_values():
    """结构层面的保证：CheckItem / BootstrapResult 没有能塞密钥的字段。"""
    assert set(CheckItem.__dataclass_fields__) == {"key", "message", "fix", "severity"}
    assert set(quickstart.BootstrapResult.__dataclass_fields__) == {"path", "created", "fields"}


def test_format_report_mentions_each_missing_name_and_the_command():
    """渲染出来的报告要含：缺的项、中文「怎么补」、以及重跑命令。"""
    items = [
        CheckItem(key=SECRET_ENV, message=f"没有设置环境变量 {SECRET_ENV}", fix=f"$env:{SECRET_ENV} = \"...\""),
    ]
    report = format_report(items, heading="密钥检查没过：")
    assert "密钥检查没过：" in report
    assert "还缺 1 项" in report
    assert SECRET_ENV in report
    assert "怎么补：" in report
    assert "python run_host.py" in report


def test_format_report_is_empty_when_everything_is_ready():
    assert format_report([]) == ""


# ---------------------------------------------------------------------------
# ensure_config_file：首次生成 + 幂等
# ---------------------------------------------------------------------------


def test_missing_config_is_generated_with_editable_fields(tmp_path):
    """没有配置 → 生成一份，并明确列出要改的字段。"""
    target = tmp_path / "host.json"
    result = ensure_config_file(target)
    assert result.created is True
    assert result.path == target
    assert target.is_file()
    # 生成的必须是**合法 JSON 且能被 HostConfig 接受**
    HostConfig.load(target)
    message = result.message
    assert "已生成配置文件" in message
    assert "请先编辑这几个字段" in message
    for field in ("owner_user_id", "device_id", "allowed_packages", "onebot.http_url"):
        assert field in message


def test_existing_config_is_never_overwritten(tmp_path):
    """已有配置（哪怕使用者改得面目全非）→ 一个字都不改。"""
    target = tmp_path / "host.json"
    edited = json.dumps(_valid_config_mapping(owner_user_id="88888", device_id="my-phone-01"), ensure_ascii=False)
    target.write_text(edited, encoding="utf-8")

    result = ensure_config_file(target)
    assert result.created is False
    assert target.read_text(encoding="utf-8") == edited
    assert "保持原样" in result.message


def test_ensure_config_file_is_idempotent(tmp_path):
    """连跑三次：只有第一次生成，字节内容始终不变。"""
    target = tmp_path / "host.json"
    first = ensure_config_file(target)
    snapshot = target.read_bytes()
    assert first.created is True
    for _ in range(2):
        again = ensure_config_file(target)
        assert again.created is False
        assert target.read_bytes() == snapshot


def test_ensure_config_file_creates_parent_directories(tmp_path):
    """父目录不存在时也要能生成（`--config configs/host.json` 这种用法）。"""
    target = tmp_path / "configs" / "deep" / "host.json"
    result = ensure_config_file(target)
    assert result.created is True
    assert target.is_file()


def test_ensure_config_file_accepts_custom_template(tmp_path):
    """自定义模板：内容来自传入的模板，而不是内置常量。"""
    target = tmp_path / "custom.json"
    template = _valid_config_mapping(owner_user_id="12345")
    ensure_config_file(target, template=template)
    assert json.loads(target.read_text(encoding="utf-8"))["owner_user_id"] == "12345"


# ---------------------------------------------------------------------------
# 配置非法：英文异常 → 中文可照做
# ---------------------------------------------------------------------------


def test_validate_config_file_reports_missing_file_in_chinese(tmp_path):
    items = validate_config_file(tmp_path / "nope.json")
    assert len(items) == 1
    assert "找不到配置文件" in items[0].message
    assert "run_host.py" in items[0].fix


def test_validate_config_file_translates_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json ", encoding="utf-8")
    items = validate_config_file(path)
    assert len(items) == 1
    assert "不是合法 JSON" in items[0].message
    assert "JSON" in items[0].message  # 保留了原始报错细节
    assert items[0].key == "config_json"


def test_validate_config_file_translates_unknown_keys(tmp_path):
    path = tmp_path / "extra.json"
    path.write_text(json.dumps({**_valid_config_mapping(), "api_key": "x"}), encoding="utf-8")
    items = validate_config_file(path)
    assert len(items) == 1
    assert "不认识的顶层字段" in items[0].message
    assert "api_key" in items[0].message


def test_validate_config_file_translates_owner_id_and_base_url(tmp_path):
    """两个最常见的写错点：QQ 号写成昵称、base_url 写成 http。"""
    raw = _valid_config_mapping(owner_user_id="鲸鱼娘")
    path = _write_config(tmp_path, raw)
    items = validate_config_file(path)
    assert "owner_user_id" in items[0].message
    assert "纯数字" in items[0].message

    raw = _valid_config_mapping()
    raw["model"]["base_url"] = "http://127.0.0.1:11434"
    path = _write_config(tmp_path, raw)
    items = validate_config_file(path)
    assert "https" in items[0].message
    assert "反代" in items[0].fix or "留空" in items[0].fix


def test_validate_config_file_reports_ok_for_valid_config(tmp_path):
    path = _write_config(tmp_path, _valid_config_mapping())
    assert validate_config_file(path) == []
    config, items = load_config(path)
    assert config is not None
    assert items == []


def test_explain_config_error_falls_back_without_losing_detail():
    """翻译表没覆盖到的错误：保留原文（英文不是密钥，没有泄漏风险）。"""
    item = explain_config_error("something unexpected happened")
    assert "something unexpected happened" in item.message
    assert "examples/host.example.json" in item.fix


def test_explain_config_error_is_pure_and_total():
    """翻译函数对任意字符串都必须返回一条可读项，绝不能抛错。"""
    for message in ("", "unknown", "ConfigError: x", "device_server.port must be an integer"):
        item = explain_config_error(message)
        assert isinstance(item, CheckItem)
        assert item.message and item.fix


def test_load_config_never_raises_config_error():
    """`load_config` 是 run_host 的入口，任何配置错误都必须变成列表而不是异常。

    注意这里刻意也覆盖**非 ConfigError** 的情形：`ModelConfig.from_mapping`
    缺 `provider_id` 时抛的是 `KeyError`，`HostConfig` 也可能抛 `OSError`
    （文件权限）——`run_host.py` 绝不能让这些裸异常冒到使用者面前。
    """
    broken = [
        {},  # 缺 provider_id → KeyError
        {"model": {}, "owner_user_id": "x", "device_id": "y", "signing_secret_env": ""},
        {"owner_user_id": "x"},  # 缺 model → ConfigError
        [],  # 顶层不是对象 → ConfigError
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for index, mapping in enumerate(broken):
            path = Path(tmp) / f"bad-{index}.json"
            path.write_text(json.dumps(mapping), encoding="utf-8")
            config, items = load_config(path)  # 不得抛异常
            assert config is None
            assert len(items) == 1
            assert items[0].message and items[0].fix
            assert not isinstance(items[0].message, Exception)


# ---------------------------------------------------------------------------
# run_host.py：端到端行为（退出码 + 真实打印）
# ---------------------------------------------------------------------------


def test_run_host_generates_config_and_exits_with_helpful_output(tmp_path, capsys):
    """真跑一次 `python run_host.py`（无配置）：生成 + 中文提示 + 退出码 1。"""
    target = tmp_path / "host.json"
    code = run_host.main(["--config", str(target)])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_NOT_READY == 1
    assert target.is_file()
    assert "[1/4]" in out and "[2/4]" in out
    assert "已生成配置文件" in out
    assert "请先编辑这几个字段" in out
    assert "owner_user_id" in out
    assert "Traceback" not in out


def test_run_host_with_bad_config_exits_2_with_chinese_reason(tmp_path, capsys):
    """配置文件写坏：退出码 2 + 中文原因，而不是 traceback。"""
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**_valid_config_mapping(), "unknown_key": 1}), encoding="utf-8")

    code = run_host.main(["--config", str(path)])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_BAD_CONFIG == 2
    assert "配置检查没过" in out
    assert "unknown_key" in out
    assert "Traceback" not in out


def test_run_host_blocks_on_missing_secret_and_never_prints_its_value(tmp_path, capsys, monkeypatch):
    """配置合法但缺密钥：退出 1，报告里只有变量名。"""
    path = _write_config(tmp_path, _valid_config_mapping())
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.setenv(MODEL_ENV, FAKE_MODEL_KEY)
    monkeypatch.setenv(ONEBOT_ENV, FAKE_ONEBOT_TOKEN)

    code = run_host.main(["--config", str(path)])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_NOT_READY == 1
    assert "密钥检查没过" in out
    assert SECRET_ENV in out
    assert "只显示环境变量名" in out
    assert FAKE_MODEL_KEY not in out
    assert FAKE_ONEBOT_TOKEN not in out
    assert "正在启动独立宿主" not in out  # 没就绪就绝不能启动


def test_run_host_check_mode_passes_and_does_not_start(tmp_path, capsys, monkeypatch):
    """全部就绪 + `--check`：退出 0，明确说明「没有真正启动」。"""
    path = _write_config(tmp_path, _valid_config_mapping())
    monkeypatch.setenv(SECRET_ENV, FAKE_SIGNING_SECRET)
    monkeypatch.setenv(MODEL_ENV, FAKE_MODEL_KEY)
    monkeypatch.setenv(ONEBOT_ENV, FAKE_ONEBOT_TOKEN)

    code = run_host.main(["--config", str(path), "--check"])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_OK == 0
    assert "[3/4]" in out and "[4/4]" in out
    assert "体检通过" in out
    assert "正在启动独立宿主" not in out
    # 尽管环境里有真实值，一个都不许印出来
    for secret in (FAKE_SIGNING_SECRET, FAKE_MODEL_KEY, FAKE_ONEBOT_TOKEN):
        assert secret not in out


def test_run_host_reports_missing_tls_files_before_starting(tmp_path, capsys, monkeypatch):
    """声明了证书但文件不存在：启动前就说清楚（而不是让 standalone 抛异常）。"""
    raw = _valid_config_mapping()
    raw["device_server"]["certfile"] = str(tmp_path / "missing-cert.pem")
    raw["device_server"]["keyfile"] = str(tmp_path / "missing-key.pem")
    path = _write_config(tmp_path, raw)
    monkeypatch.setenv(SECRET_ENV, FAKE_SIGNING_SECRET)
    monkeypatch.setenv(MODEL_ENV, FAKE_MODEL_KEY)
    monkeypatch.setenv(ONEBOT_ENV, FAKE_ONEBOT_TOKEN)

    code = run_host.main(["--config", str(path), "--check"])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_START_FAILED == 3
    assert "missing-cert.pem" in out
    assert "certfile" in out


def test_run_host_default_config_path_is_inside_host_dir():
    """不给 --config 时默认就是 host/host.json（使用者不用记路径）。"""
    path = run_host._resolve_config_path(None)
    assert path.name == "host.json"
    assert path.parent == run_host._HOST_DIR
    assert (run_host._HOST_DIR / "run_host.py").is_file()


def test_run_host_propagates_passthrough_flags():
    """`--once` / `--interval` 要传给 standalone.main，而不是被脚本吞掉。"""
    args = run_host._parse_args(["--config", "x.json", "--once", "--interval", "0.5"])
    assert args.once is True
    assert args.interval == 0.5
    assert args.check is False


# ---------------------------------------------------------------------------
# run_host → standalone.main：真的把启动交出去
# ---------------------------------------------------------------------------


def test_run_host_calls_standalone_main_with_config_path(tmp_path, capsys, monkeypatch):
    """全部就绪时，唯一的启动动作是调 `standalone.main`，并带上配置路径与透传参数。"""
    from astrbot_whale_device import standalone

    path = _write_config(tmp_path, _valid_config_mapping())
    monkeypatch.setenv(SECRET_ENV, FAKE_SIGNING_SECRET)
    monkeypatch.setenv(MODEL_ENV, FAKE_MODEL_KEY)
    monkeypatch.setenv(ONEBOT_ENV, FAKE_ONEBOT_TOKEN)

    calls: list[list[str]] = []

    def fake_main(argv=None):
        calls.append(list(argv or []))
        return 0

    monkeypatch.setattr(standalone, "main", fake_main)
    code = run_host.main(["--config", str(path), "--once", "--interval", "0.5"])
    out = capsys.readouterr().out

    assert code == 0
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "--config"
    assert argv[1] == str(path)
    assert "--once" in argv
    assert argv[argv.index("--interval") + 1] == "0.5"
    assert "正在启动独立宿主" in out
    for secret in (FAKE_SIGNING_SECRET, FAKE_MODEL_KEY, FAKE_ONEBOT_TOKEN):
        assert secret not in out


def test_run_host_turns_standalone_crash_into_human_message(tmp_path, capsys, monkeypatch):
    """standalone 抛任何异常都不能漏 traceback，必须是退出码 3 + 人话。"""
    from astrbot_whale_device import standalone

    path = _write_config(tmp_path, _valid_config_mapping())
    monkeypatch.setenv(SECRET_ENV, FAKE_SIGNING_SECRET)
    monkeypatch.setenv(MODEL_ENV, FAKE_MODEL_KEY)
    monkeypatch.setenv(ONEBOT_ENV, FAKE_ONEBOT_TOKEN)

    def boom(argv=None):
        raise RuntimeError("websockets runtime is required for the host WSS device server")

    monkeypatch.setattr(standalone, "main", boom)
    code = run_host.main(["--config", str(path)])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_START_FAILED == 3
    assert "启动失败" in out
    assert "RuntimeError" in out            # 保留异常类型，方便排查
    assert "websockets runtime is required" in out
    assert "Traceback" not in out
    assert "run_host.py --check" in out


def test_run_host_explains_missing_tls_cert_from_standalone(tmp_path, capsys, monkeypatch):
    """standalone 抛的 FileNotFoundError（证书）要走专门的「改 certfile」提示。"""
    from astrbot_whale_device import standalone

    path = _write_config(tmp_path, _valid_config_mapping())
    monkeypatch.setenv(SECRET_ENV, FAKE_SIGNING_SECRET)
    monkeypatch.setenv(MODEL_ENV, FAKE_MODEL_KEY)
    monkeypatch.setenv(ONEBOT_ENV, FAKE_ONEBOT_TOKEN)

    def boom(argv=None):
        raise FileNotFoundError("TLS files declared but missing: ['nope.pem']")

    monkeypatch.setattr(standalone, "main", boom)
    code = run_host.main(["--config", str(path)])
    out = capsys.readouterr().out

    assert code == run_host.EXIT_START_FAILED == 3
    assert "certfile" in out and "keyfile" in out
    assert "Traceback" not in out
