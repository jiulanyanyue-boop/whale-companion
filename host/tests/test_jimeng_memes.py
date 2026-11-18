import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrbot_whale_device.jimeng_client import Credentials, CredentialsMissing, JimengClient, JimengError, _sign_headers
from astrbot_whale_device.memes.library import MemeLibrary
from astrbot_whale_device.memes.references import fixed_reference, ReferenceUploadNotConfigured, TOSReferenceUploader


def test_missing_credentials_are_explicit(monkeypatch):
    monkeypatch.delenv("VOLCENGINE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("VOLCENGINE_SECRET_KEY", raising=False)
    with pytest.raises(CredentialsMissing):
        Credentials.from_env()


def test_signed_submit_uses_official_action_and_never_retries():
    seen = []
    def transport(url, headers, body, timeout):
        seen.append((url, headers, json.loads(body)))
        return b'{"data":{"task_id":"task-1"}}'
    client = JimengClient(Credentials("ak", "sk"), transport=transport)
    result = client.submit("hello")
    assert result["data"]["task_id"] == "task-1"
    assert len(seen) == 1
    url, headers, payload = seen[0]
    assert "CVSync2AsyncSubmitTask" in url and "2022-08-31" in url
    assert payload == {"req_key": "jimeng_t2i_v40", "prompt": "hello", "force_single": True}
    assert headers["Authorization"].startswith("HMAC-SHA256 Credential=ak/")


def test_submit_transport_failure_not_retried_and_secret_not_leaked():
    calls = []
    def transport(*args):
        calls.append(1)
        raise RuntimeError("secret-sk-placeholder")
    client = JimengClient(Credentials("ak", "sk"), transport=transport)
    with pytest.raises(JimengError) as error:
        client.submit("x")
    assert len(calls) == 1
    assert "secret-sk-placeholder" not in str(error.value)


def test_library_deduplicates_and_ranks_metadata(tmp_path):
    library = MemeLibrary(tmp_path)
    digest = library.add(b"png bytes", description="柴犬震惊", image_text="啊？", meaning="表达意外", emotion="惊讶", scene="反应")
    assert library.add(b"png bytes", description="ignored") == digest
    found = library.search("柴犬 惊讶", minimum_score=0.1)
    assert len(found) == 1
    assert Path(found[0]["path"]).read_bytes() == b"png bytes"
    assert found[0]["score"] > 0


def test_client_polling_is_bounded():
    count = []
    def transport(url, headers, body, timeout):
        count.append(1)
        return b'{"data":{"status":"processing"}}'
    client = JimengClient(Credentials("ak", "sk"), transport=transport, max_polls=2, poll_interval=0)
    with pytest.raises(JimengError, match="次数上限"):
        client.wait_result("task")
    assert len(count) == 2


def test_sign_is_deterministic_for_fixed_clock():
    first = _sign_headers(b"{}", Credentials("a", "s"), datetime(2026, 1, 1, tzinfo=timezone.utc), action="CVSync2AsyncSubmitTask")
    second = _sign_headers(b"{}", Credentials("a", "s"), datetime(2026, 1, 1, tzinfo=timezone.utc), action="CVSync2AsyncSubmitTask")
    assert first == second


def test_chinese_substring_search_and_search_does_not_mark_used(tmp_path):
    library = MemeLibrary(tmp_path)
    digest = library.add(b"image", description="\u84dd\u53d1\u9cb8\u9c7c\u5a18\u5f00\u5fc3\u5730\u7b11", meaning="\u5f00\u5fc3\u56de\u5e94")
    found = library.search("\u9cb8\u9c7c \u5f00\u5fc3")
    assert found and found[0]["digest"] == digest
    assert found[0]["last_used"] == 0
    library.mark_used(digest)
    assert library.search("\u9cb8\u9c7c")[0]["last_used"] > 0


def test_generation_budget_and_duplicate_cooldown(tmp_path):
    library = MemeLibrary(tmp_path, daily_generation_limit=1, cooldown_seconds=90)
    assert library.generation_allowed("\u505a\u4e00\u4e2a\u5f00\u5fc3\u8868\u60c5", now=1000) == (True, "reserved")
    assert library.generation_allowed("\u505a\u4e00\u4e2a\u5f00\u5fc3\u8868\u60c5", now=1050) == (False, "duplicate_cooldown")
    assert library.generation_allowed("\u65b0\u7684\u4e0d\u540c\u573a\u666f", now=1200) == (False, "daily_generation_limit")


def test_result_query_is_signed_for_its_action():
    seen = []
    def transport(url, headers, body, timeout):
        seen.append(url)
        return b'{"code":10000,"data":{"status":"done"}}'
    client = JimengClient(Credentials("ak", "sk"), transport=transport)
    client.get_result("task-1")
    assert "CVSync2AsyncGetResult" in seen[0]


def test_reference_submit_contains_exactly_one_image_url():
    seen = []
    def transport(url, headers, body, timeout):
        seen.append(json.loads(body))
        return b'{"data":{"task_id":"task-1"}}'
    JimengClient(Credentials("ak", "sk"), transport=transport).submit(
        "draw", verified_fields={"image_urls": ["https://signed.example/ref.png"]})
    assert seen[0]["image_urls"] == ["https://signed.example/ref.png"]
    assert "binary_data_base64" not in seen[0]


def test_reference_upload_requires_private_tos_configuration(tmp_path):
    ref = fixed_reference(tmp_path, "gpt") if False else None
    uploader = TOSReferenceUploader()
    with pytest.raises(ReferenceUploadNotConfigured, match="reference_upload_not_configured"):
        uploader.upload(ref)  # type: ignore[arg-type]
