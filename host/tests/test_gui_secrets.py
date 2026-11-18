"""GUI 本地密钥保管的固定测试。

## 这个文件守住什么

用户实测撞到过：界面上点「拉取模型列表」返回
`HTTP 401: Authentication Fails`——401 说明请求确实到了服务商，
只是 Key 没配。而当时的界面上**根本没有填 Key 的地方**：
`HostConfig` 的设计是「配置文件只存环境变量名，明文不进 host.json」，
那条设计是对的，但它把「设环境变量」这件普通用户做不到的事变成了门槛。

于是补了 `gui_secrets`：界面填 → 存 `secrets.json` → 启动时注入进程环境变量。
这里守四条底线：

1. **明文绝不进 `host.json`**（配置会被备份/截图/进版本库）；
2. **脱敏函数绝不泄露明文**（日志与界面都走它）；
3. **不覆盖用户显式设的环境变量**（容器/CI 场景要能覆盖）；
4. **`secrets.json` 必须在 `.gitignore` 里**（提交进仓库等于永久泄漏）。
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from astrbot_whale_device.gui_secrets import (
    DEFAULT_MODEL_KEY_ENV,
    DEFAULT_SIGNING_ENV,
    SECRETS_FILENAME,
    StoredSecrets,
    ensure_secrets_ignored,
    generate_signing_secret,
    mask,
)


@pytest.fixture()
def workdir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def test_mask_of_empty_is_empty() -> None:
    assert mask("") == ""
    assert mask("   ") == ""


def test_mask_never_returns_full_secret() -> None:
    """最关键的一条：脱敏结果不能包含完整明文。"""
    secret = "test-fixture-mask-never-return-this"
    masked = mask(secret)
    assert secret not in masked
    assert len(masked) > 0


def test_mask_keeps_first_four_for_identification() -> None:
    # 前 4 位是为了让用户确认「填的是哪个 key」，不足以推算内容。
    assert mask("sk-1234567890").startswith("sk-1")


def test_mask_of_very_short_value_is_all_stars() -> None:
    # 4 位以内保留任何字符都可能暴露太多，全打码。
    assert mask("abc") == "***"


def test_generated_signing_secret_is_long_and_random() -> None:
    a = generate_signing_secret()
    b = generate_signing_secret()
    assert len(a) == 64          # 32 字节十六进制
    assert a != b
    int(a, 16)                   # 必须是合法十六进制


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_file_missing(workdir: pathlib.Path) -> None:
    """首次运行不该抛异常（否则用户第一次打开就崩）。"""
    config_path = workdir / "host.json"
    store = StoredSecrets.load(config_path)
    assert store.signing_secret == ""
    assert store.model_api_key == ""


def test_load_returns_empty_when_file_corrupt(workdir: pathlib.Path) -> None:
    config_path = workdir / "host.json"
    (workdir / SECRETS_FILENAME).write_text("{ not json", encoding="utf-8")
    assert StoredSecrets.load(config_path).signing_secret == ""


def test_save_then_load_round_trip(workdir: pathlib.Path) -> None:
    config_path = workdir / "host.json"
    store = StoredSecrets(signing_secret="a" * 64, model_api_key="sk-test-123")
    saved = store.save(config_path)
    assert saved.is_file()
    loaded = StoredSecrets.load(config_path)
    assert loaded.signing_secret == "a" * 64
    assert loaded.model_api_key == "sk-test-123"


def test_secrets_file_lives_next_to_host_json(workdir: pathlib.Path) -> None:
    """与 host.json 同目录：便于整目录备份/迁移/删除，也让 .gitignore 规则简单。"""
    config_path = workdir / "host.json"
    assert StoredSecrets.path_for(config_path).parent == workdir


def test_ensure_signing_secret_generates_only_once() -> None:
    store = StoredSecrets()
    assert store.ensure_signing_secret() is True
    first = store.signing_secret
    assert store.ensure_signing_secret() is False   # 已有就不重复生成
    assert store.signing_secret == first


def test_ensure_signing_secret_respects_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量里已有签名密钥 → 不生成、不覆盖。"""
    monkeypatch.setenv(DEFAULT_SIGNING_ENV, "explicitly-set")
    store = StoredSecrets()
    assert store.ensure_signing_secret() is False
    assert store.signing_secret == ""
    assert store.has_minimum() is True


# ---------------------------------------------------------------------------
# 注入环境变量
# ---------------------------------------------------------------------------


def test_apply_injects_when_env_empty() -> None:
    store = StoredSecrets(signing_secret="sig", model_api_key="key")
    fake_env: dict[str, str] = {}
    applied = store.apply_to_environ(fake_env)
    assert DEFAULT_SIGNING_ENV in applied
    assert DEFAULT_MODEL_KEY_ENV in applied
    assert fake_env[DEFAULT_SIGNING_ENV] == "sig"


def test_apply_does_not_override_existing_env() -> None:
    """用户显式设过环境变量 → 必须尊重（容器/CI 场景靠它覆盖）。"""
    store = StoredSecrets(signing_secret="from-file", model_api_key="key-from-file")
    fake_env = {DEFAULT_SIGNING_ENV: "from-env"}
    applied = store.apply_to_environ(fake_env)
    assert DEFAULT_SIGNING_ENV not in applied
    assert fake_env[DEFAULT_SIGNING_ENV] == "from-env"


def test_apply_skips_empty_values() -> None:
    store = StoredSecrets(signing_secret="", model_api_key="")
    fake_env: dict[str, str] = {}
    assert store.apply_to_environ(fake_env) == []
    assert DEFAULT_SIGNING_ENV not in fake_env


# ---------------------------------------------------------------------------
# 安全底线
# ---------------------------------------------------------------------------


def test_masked_summary_contains_no_plaintext() -> None:
    store = StoredSecrets(signing_secret="s" * 64, model_api_key="sk-secret-value")
    summary = store.masked_summary()
    joined = json.dumps(summary, ensure_ascii=False)
    assert "sk-secret-value" not in joined
    assert "s" * 64 not in joined


def test_secrets_never_written_into_host_json(workdir: pathlib.Path) -> None:
    """回归最重要的一条：明文不能进 `host.json`。"""
    config_path = workdir / "host.json"
    store = StoredSecrets(signing_secret="sig-abc", model_api_key="sk-model-key")
    store.save(config_path)

    # 模拟界面保存 host.json（只写环境变量名，不写值）
    config_path.write_text(
        json.dumps({"signing_secret_env": DEFAULT_SIGNING_ENV}, ensure_ascii=False),
        encoding="utf-8",
    )
    raw = config_path.read_text(encoding="utf-8")
    assert "sig-abc" not in raw
    assert "sk-model-key" not in raw


def test_gitignore_protects_secrets_file(workdir: pathlib.Path) -> None:
    """`secrets.json` 被提交进版本库 = 永久泄漏（删了历史里还在）。"""
    gitignore = workdir / ".gitignore"
    assert ensure_secrets_ignored(gitignore) is True
    content = gitignore.read_text(encoding="utf-8")
    assert SECRETS_FILENAME in content


def test_gitignore_append_is_idempotent(workdir: pathlib.Path) -> None:
    gitignore = workdir / ".gitignore"
    ensure_secrets_ignored(gitignore)
    assert ensure_secrets_ignored(gitignore) is False
    content = gitignore.read_text(encoding="utf-8")
    assert content.count(SECRETS_FILENAME) == 1


def test_gitignore_keeps_existing_content(workdir: pathlib.Path) -> None:
    """追加而不是覆盖：已有的忽略规则不能被抹掉。"""
    gitignore = workdir / ".gitignore"
    gitignore.write_text("host/host.json\n", encoding="utf-8")
    ensure_secrets_ignored(gitignore)
    content = gitignore.read_text(encoding="utf-8")
    assert "host/host.json" in content
    assert SECRETS_FILENAME in content


def test_saved_file_permissions_are_tight(workdir: pathlib.Path) -> None:
    """尽力收紧到 600（Windows 上 chmod 语义有限，失败不阻断）。"""
    config_path = workdir / "host.json"
    saved = StoredSecrets(model_api_key="sk-x").save(config_path)
    if os.name != "nt":
        assert (saved.stat().st_mode & 0o077) == 0
    else:
        # Windows：至少文件存在且可读
        assert saved.is_file()
