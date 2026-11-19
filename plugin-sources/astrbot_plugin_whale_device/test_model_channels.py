import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from model_channels import DSH_KEY_ENV, DSH_PROVIDER_ID, ModelChannels
from ofm_gateway import read_ofm_gateway


@pytest.fixture(autouse=True)
def isolate_dsh_home(monkeypatch, tmp_path):
    """让模型目录测试不读取本机 DSH 配置或发送真实网关请求。"""
    monkeypatch.setenv("DSH_HOME", str(tmp_path))


class Config(dict):
    fail_once = False

    def save_config(self):
        if self.fail_once:
            self.fail_once = False
            raise OSError("disk unavailable")


def make_service(monkeypatch):
    monkeypatch.setenv("DSH_OPENAI_GATEWAY_API_KEY", "private-test-key")
    monkeypatch.setenv("DSH_OPENAI_GATEWAY_PORT", "8326")
    native = {
        "id": "whale_chat",
        "enable": True,
        "model": "native",
        "key": ["native-secret"],
    }
    default = Config(
        provider=[native],
        agent_runner={
            "runner_type": "local",
            "config": {"model": {"provider_id": "whale_chat"}},
        },
    )
    owner = Config(agent_runner=copy.deepcopy(default["agent_runner"]))
    group = Config(agent_runner=copy.deepcopy(default["agent_runner"]))
    owner["agent_runner"]["config"]["persona"] = {"persona_id": "whale_owner"}
    group["agent_runner"]["config"]["persona"] = {"persona_id": "whale_group"}
    unrelated = Config(agent_runner=copy.deepcopy(default["agent_runner"]))
    configs = {
        "default": default,
        "owner": owner,
        "group": group,
        "unrelated": unrelated,
    }
    manager = SimpleNamespace(inst_map={"whale_chat": object()})

    async def create(config):
        default["provider"].append(config)
        manager.inst_map[config["id"]] = object()

    async def update(identifier, config):
        default["provider"] = [
            config if p["id"] == identifier else p for p in default["provider"]
        ]
        manager.inst_map[identifier] = object()

    manager.create_provider = AsyncMock(side_effect=create)
    manager.update_provider = AsyncMock(side_effect=update)
    context = SimpleNamespace(
        get_config=lambda: default,
        provider_manager=manager,
        astrbot_config_mgr=SimpleNamespace(confs=configs),
    )
    service = ModelChannels(
        SimpleNamespace(
            context=context,
            config={},
        )
    )
    return service, configs, manager


def test_ofm_gateway_keeps_model_ids_and_uses_its_own_provider(monkeypatch, tmp_path):
    """分离两个网关的凭据和地址，原样使用不带或带斜线的 OFM 模型 ID。"""
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.delenv("WHALE_OFM_MODEL_KEY", raising=False)
    settings = tmp_path / "our-free-model" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"forwardKey": "ofm-private-test-key", "forward": {
        "enabled": True, "host": "127.0.0.1", "port": 18917,
    }}), encoding="utf-8")
    service, profiles, manager = make_service(monkeypatch)
    calls = []

    class Response:
        status = 200

        def __init__(self, rows):
            self.rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"data": self.rows}

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append((url, kwargs["headers"]["Authorization"]))
            if ":8326/" in url:
                return Response([{"id": "qoder/auto"}, {"id": "our-free-model/kilo/model:free"}])
            return Response([{"id": "deepseek-flash-free"}, {"id": "kilo/model:free"}])

    monkeypatch.setattr("model_channels.aiohttp.ClientSession", Session)
    catalog = asyncio.run(service.catalog())
    assert [(p["channel"], p["model"]) for p in catalog["choices"]] == [
        ("astrbot", "whale_chat"), ("qoder", "qoder/auto"),
        ("ofm-local", "deepseek-flash-free"), ("ofm-local", "kilo/model:free"),
    ]
    assert calls == [("http://127.0.0.1:8326/v1/models", "Bearer private-test-key"),
                     ("http://127.0.0.1:18917/v1/models", "Bearer ofm-private-test-key")]
    assert "private-test-key" not in json.dumps(catalog)
    assert not catalog["ofm_gateway_error"]
    asyncio.run(service.select("qoder", "qoder/auto"))
    before = copy.deepcopy(profiles["default"]["provider"][-1])
    updated = asyncio.run(service.select("ofm-local", "kilo/model:free"))
    assert profiles["default"]["provider"][-2] == before
    saved = manager.create_provider.await_args.args[0]
    assert saved["id"] == "whale_ofm"
    assert saved["api_base"] == "http://127.0.0.1:18917/v1"
    assert saved["model"] == "kilo/model:free"
    assert saved["key"] == ["$WHALE_OFM_MODEL_KEY"]
    assert updated["selections"][0]["channel"] == "ofm-local"
    with pytest.raises(ValueError):
        asyncio.run(service.accounts("ofm-local"))
    settings.unlink()
    fallback = asyncio.run(service.catalog())
    assert any(p["model"] == "our-free-model/kilo/model:free" for p in fallback["choices"])
    assert not fallback["ofm_online"]


@pytest.mark.parametrize("override", [{"host": "0.0.0.0"}, {"port": True}, {"enabled": False}])
def test_invalid_ofm_config_never_returns_credentials(tmp_path, override):
    """禁用、非回环或畸形端口一律拒绝，错误中不包含转发凭据。"""
    settings = tmp_path / "our-free-model" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"forwardKey": "not-for-diagnostics", "forward": {
        "enabled": True, "host": "127.0.0.1", "port": 18899, **override,
    }}), encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        read_ofm_gateway()
    assert "not-for-diagnostics" not in str(failure.value)


def choices():
    return {
        "choices": [
            {"channel": "trae", "model": "trae/test"},
            {"channel": "astrbot", "model": "whale_chat"},
        ],
        "selections": [],
    }


def test_switch_unifies_official_profiles_and_compression_without_copying_key(
    monkeypatch,
):
    service, profiles, manager = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    asyncio.run(service.select("trae", "trae/test"))
    for name in ("default", "owner", "group"):
        settings = profiles[name]["agent_runner"]["config"]
        assert settings["model"]["provider_id"] == DSH_PROVIDER_ID
        assert settings["compression"]["provider_id"] == DSH_PROVIDER_ID
    assert (
        profiles["unrelated"]["agent_runner"]["config"]["model"]["provider_id"]
        == "whale_chat"
    )
    saved = profiles["default"]["provider"][-1]
    assert saved["key"] == ["$" + DSH_KEY_ENV]
    assert "private-test-key" not in json.dumps(saved)
    asyncio.run(service.select("astrbot", "whale_chat"))
    assert (
        profiles["owner"]["agent_runner"]["config"]["model"]["provider_id"]
        == "whale_chat"
    )
    assert profiles["default"]["provider"][0]["key"] == ["native-secret"]
    manager.create_provider.assert_awaited_once()


def test_catalog_only_reads_metadata_and_never_returns_secrets(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {
                "data": [
                    {
                        "id": "trae/test",
                        "name": "Test ×0.39",
                        "input": ["text", "image"],
                    },
                    {"id": "qoder/auto"},
                    {"id": "qoder/kmodel", "name": "Kimi-K2.8-Preview · x0.8"},
                    {"id": "qoder/qmodel", "name": "Qwen3.7-Plus · x0.1→x0.04"},
                    {"id": "qoder/qfmodel", "name": "Qwen3.8-Flash · 免费"},
                    {"id": 17},
                ]
            }

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def get(self, url, **kwargs):
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            return Response()

    monkeypatch.setattr("model_channels.aiohttp.ClientSession", Session)
    data = asyncio.run(service.catalog())
    assert calls == ["http://127.0.0.1:8326/v1/models"]
    assert len(data["choices"]) == 6
    assert "image" in data["choices"][1]["modalities"]
    assert data["choices"][1]["multiplier"] == 0.39
    assert data["choices"][2]["multiplier"] is None
    assert data["choices"][2]["name"] == "auto"
    kimi, qwen, free = data["choices"][3:]
    assert kimi["name"] == "Kimi-K2.8-Preview · x0.8"
    assert kimi["model"] == "qoder/kmodel"
    assert qwen["pricing_label"] == "x0.1→x0.04"
    assert qwen["multiplier"] is None
    assert free["pricing_label"] == "免费"
    assert "image" not in data["choices"][2]["modalities"]
    assert not data["gateway_error"]
    assert "secret" not in json.dumps(data)
    assert "private-test-key" not in json.dumps(data)


def test_offline_gateway_keeps_native_choice_and_rejects_stale_model(monkeypatch):
    service, _, manager = make_service(monkeypatch)
    monkeypatch.delenv(DSH_KEY_ENV, raising=False)
    data = asyncio.run(service.catalog())
    assert data["gateway_error"]
    assert [p["channel"] for p in data["choices"]] == ["astrbot"]
    with pytest.raises(ValueError):
        asyncio.run(service.select("trae", "trae/stale"))
    manager.create_provider.assert_not_awaited()
    asyncio.run(service.select("astrbot", "whale_chat"))


def test_profile_save_failure_restores_all_profile_selections(monkeypatch):
    service, profiles, _ = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    profiles["group"].fail_once = True
    with pytest.raises(OSError):
        asyncio.run(service.select("trae", "trae/test"))
    for profile in profiles.values():
        assert profile["agent_runner"]["config"]["model"]["provider_id"] == "whale_chat"


def test_reload_failure_restores_existing_dsh_provider(monkeypatch):
    service, profiles, manager = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    asyncio.run(service.select("trae", "trae/test"))
    previous = copy.deepcopy(profiles["default"]["provider"][-1])
    manager.update_provider.side_effect = [RuntimeError("cannot load adapter"), None]
    with pytest.raises(RuntimeError):
        asyncio.run(service.select("trae", "trae/test"))
    assert manager.update_provider.await_args_list[-1].args == (
        DSH_PROVIDER_ID,
        previous,
    )


def test_external_agent_rejected_before_changing_provider(monkeypatch):
    service, profiles, manager = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    profiles["group"]["agent_runner"]["runner_type"] = "external"
    with pytest.raises(ValueError):
        asyncio.run(service.select("trae", "trae/test"))
    manager.create_provider.assert_not_awaited()


def test_local_gateway_bypasses_system_proxy_without_losing_existing_exclusions(
    monkeypatch,
):
    monkeypatch.setenv("NO_PROXY", "example.test,intranet.test")
    monkeypatch.setenv("no_proxy", "example.test,intranet.test")
    make_service(monkeypatch)
    import os

    expected = "example.test,intranet.test,127.0.0.1,localhost,::1"
    assert os.environ["NO_PROXY"] == expected
    assert os.environ["no_proxy"] == expected
    make_service(monkeypatch)
    assert os.environ["NO_PROXY"] == expected


def test_account_actions_keep_unknown_balance_and_partial_claim_results(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    calls = []

    async def rpc(method, payload):
        calls.append((method, payload))
        if method == "account.list":
            return {
                "accounts": [
                    {
                        "id": "a",
                        "nickname": "Alice",
                        "refreshable": True,
                        "credentialRef": "must-not-leak",
                    },
                    {"id": "b", "refreshable": False},
                ]
            }
        if method == "account.refresh":
            return {"success": True}
        if method == "credits.balances":
            return {
                "accounts": [
                    {"accountId": "a", "balance": {"total": 12.5, "packages": []}},
                    {"accountId": "b", "balance": None},
                ]
            }
        if method == "credits.status":
            return {
                "accounts": [
                    {
                        "accountId": "a",
                        "status": {
                            "active": True,
                            "todayCheckedIn": False,
                            "dailyCredit": 100,
                        },
                    }
                ]
            }
        if method == "usage.autoCheckin":
            return {"autoCheckin": {"enabled": True}}
        if method == "credits.claimAll":
            return {
                "summary": {"claimed": 1, "totalCredit": 100, "failed": 1},
                "results": [
                    {
                        "accountId": "b",
                        "outcome": {"kind": "failed", "coversToday": False},
                    }
                ],
            }

    service.account_rpc = rpc
    data = asyncio.run(service.accounts("trae"))
    assert data["accounts"][0]["balance"]["total"] == 12.5
    assert data["accounts"][1]["balance"] is None
    assert "credentialRef" not in json.dumps(data)
    assert not any(method == "credits.claimAll" for method, _ in calls)
    with pytest.raises(ValueError):
        asyncio.run(service.accounts("trae", "renew", "b"))
    data = asyncio.run(service.accounts("trae", "renew", "a"))
    assert data["receipt"]["success"] is True
    data = asyncio.run(service.accounts("trae", "checkin"))
    assert data["receipt"]["summary"]["failed"] == 1
    assert data["receipt"]["results"][0]["covers_today"] is False
    with pytest.raises(ValueError):
        asyncio.run(service.accounts("trae", "autocheckin", enabled="true"))
    asyncio.run(service.accounts("trae", "autocheckin", enabled=False))
    assert ("usage.autoCheckin", {"enabled": False}) in calls


def test_failed_account_renewal_is_never_reported_as_success(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    service.account_rpc = AsyncMock(
        side_effect=[
            {"accounts": [{"id": "a", "refreshable": True}]},
            {"success": False},
        ]
    )
    with pytest.raises(RuntimeError):
        asyncio.run(service.accounts("trae", "renew", "a"))
