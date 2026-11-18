"""`model_client` 的真回环测试。

本文件**不 mock HTTP**：每个用例都用标准库 `http.server` 在 127.0.0.1 上起一个
真实的 HTTP 服务，客户端通过 `urllib.request` 真发真收，断言的是服务端真实收到的
路径/请求头/请求体与真实返回的响应。
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from astrbot_whale_device import model_client
from astrbot_whale_device.astrbot_adapter import ModelGateway
from astrbot_whale_device.models import MODEL_TIMEOUT_MS, ModelConfig
from astrbot_whale_device.router import ModelTimeoutError

# 长且显式标记的假密钥：便于断言「任何地方都不出现」。
KEY = "test-fixture-no-real-credential"
KEY_FRAGMENT = "no-real-credential"


# ---------------------------------------------------------------------------
# 真回环服务
# ---------------------------------------------------------------------------

Responder = Callable[[dict[str, Any]], tuple[int, str, dict[str, str] | None, float]]


class LoopbackServer:
    """127.0.0.1 上的真实 HTTP 服务，记录每一条收到的请求。"""

    def __init__(self, responder: Responder) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder = responder
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的固定钩子名
                length = int(self.headers.get("Content-Length") or 0)
                entry = {
                    "path": self.path,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": self.rfile.read(length) if length else b"",
                }
                outer.requests.append(entry)
                status, data, extra_headers, delay = outer.responder(entry)
                if delay:
                    time.sleep(delay)
                body = data if isinstance(data, bytes) else data.encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for name, value in (extra_headers or {}).items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except OSError:
                    # 客户端超时/提前断开时写回会失败，这属于被测行为本身。
                    pass

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextlib.contextmanager
def loopback(responder: Responder):
    server = LoopbackServer(responder)
    try:
        yield server
    finally:
        server.close()


def _content_response(content: str, *, status: int = 200) -> Responder:
    def responder(_entry: dict[str, Any]):
        payload = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
        return status, json.dumps(payload, ensure_ascii=False), None, 0.0

    return responder


def _raw_response(
    text: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    delay: float = 0.0,
) -> Responder:
    def responder(_entry: dict[str, Any]):
        return status, text, headers, delay

    return responder


def _payload(base_url: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider_id": "openai-compat",
        "base_url": base_url,
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "你好呀"}],
        "max_context_tokens": 1024,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 正常路径：真发真收
# ---------------------------------------------------------------------------


def test_transport_posts_to_fixed_path_with_bearer_and_returns_raw_text():
    # 返回的不是 JSON 决策：证明客户端只搬文本、不解析。
    raw_text = "  这不是 JSON，也不是决策  \n"
    with loopback(_content_response(raw_text)) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        result = transport(_payload(server.base_url))

        assert result == raw_text, "模型文本必须原样返回"
        assert len(server.requests) == 1, "一次调用只发一次请求（禁止自动重试）"
        sent = server.requests[0]
        assert sent["path"] == "/v1/chat/completions"
        assert sent["headers"]["authorization"] == f"Bearer {KEY}"
        assert sent["headers"]["content-type"] == "application/json"
        assert KEY not in sent["path"] and KEY not in sent["headers"].get("user-agent", "")
        body = json.loads(sent["body"].decode("utf-8"))
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [{"role": "user", "content": "你好呀"}]
        assert body["stream"] is False


@pytest.mark.parametrize(
    ("suffix", "expected_path"),
    [
        ("", "/v1/chat/completions"),
        ("/", "/v1/chat/completions"),
        ("/v1", "/v1/chat/completions"),
        ("/v1/", "/v1/chat/completions"),
        ("//v1//", "/v1/chat/completions"),
        ("/v1/v1", "/v1/chat/completions"),
        ("/v1/chat/completions", "/v1/chat/completions"),
        ("/proxy/v1", "/proxy/v1/chat/completions"),
    ],
)
def test_base_url_variants_collapse_to_exactly_one_v1(suffix: str, expected_path: str):
    with loopback(_content_response("ok")) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        assert transport(_payload(server.base_url + suffix)) == "ok"
        assert server.requests[0]["path"] == expected_path


def test_resolver_credential_is_used_and_wrong_provider_fails_before_request():
    with loopback(_content_response("ok")) as server:
        transport = model_client.make_model_transport(
            api_key=lambda provider_id: KEY if provider_id == "openai-compat" else None,
            timeout_ms=2_000,
        )
        assert transport(_payload(server.base_url)) == "ok"
        assert server.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"

        with pytest.raises(model_client.ModelEndpointError):
            transport(_payload(server.base_url, provider_id="unknown"))
        assert len(server.requests) == 1, "凭据不可用时必须在发请求之前失败"


def test_gateway_accepts_the_real_transport():
    """与 `ModelGateway(transport=...)` 的真实接线：真回环、真 HTTP、真文本。"""
    raw_text = "  随便一段模型文本，必须原样落到上层  "
    with loopback(_content_response(raw_text)) as server:
        # 配置面只允许 https；真实出网 endpoint 由宿主注入的 transport 固定（此处为回环）。
        config = ModelConfig(
            provider_id="openai-compat",
            model_id="gpt-4o-mini",
            base_url="https://gateway.example.com/v1",
            fallback_model_id="gpt-4o",
        )
        transport = model_client.make_model_transport(
            api_key=KEY, base_url=server.base_url, timeout_ms=2_000
        )
        gateway = ModelGateway(config, transport)
        assert gateway.request_with_fallback([{"role": "user", "content": "hi"}]) == raw_text
        assert len(server.requests) == 1, "主备切换只在失败时发生，成功路径不得重发"
        assert server.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"


def test_default_timeout_reuses_models_constant():
    assert MODEL_TIMEOUT_MS == 30_000
    assert model_client.make_model_transport(api_key=KEY).timeout_ms == MODEL_TIMEOUT_MS
    assert issubclass(model_client.ModelClientError, ModelTimeoutError)


# ---------------------------------------------------------------------------
# 失败路径：真回环
# ---------------------------------------------------------------------------


def test_timeout_is_surfaced_as_error_not_empty_string():
    with loopback(_raw_response("late", delay=1.2)) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=150)
        with pytest.raises(model_client.ModelTransportTimeout) as info:
            transport(_payload(server.base_url))
        assert isinstance(info.value, ModelTimeoutError), "必须能被 MessageRouter 识别"
        assert "timed out" in str(info.value)
        assert KEY not in str(info.value)


def test_http_404_body_echoing_the_key_is_redacted():
    leaked = json.dumps({"error": {"message": f"invalid api_key {KEY}"}}, ensure_ascii=False)
    with loopback(_raw_response(leaked, status=404)) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        with pytest.raises(model_client.ModelHTTPStatusError) as info:
            transport(_payload(server.base_url))
        assert info.value.status == 404
        assert len(server.requests) == 1, "4xx 不得触发自动重试"
        message = str(info.value)
        assert "HTTP 404" in message
        assert KEY not in message
        assert KEY_FRAGMENT not in message


def test_http_500_body_echoing_the_authorization_header_is_redacted():
    leaked = f"upstream said: Authorization: Bearer {KEY}"
    with loopback(_raw_response(leaked, status=500)) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        with pytest.raises(model_client.ModelHTTPStatusError) as info:
            transport(_payload(server.base_url))
        assert info.value.status == 500
        message = str(info.value)
        assert KEY not in message
        assert f"Bearer {KEY}" not in message
        assert model_client.REDACTED in message


def test_non_json_body_is_rejected_and_redacted():
    with loopback(_raw_response(f"<html>bad gateway; api_key={KEY}</html>")) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        with pytest.raises(model_client.ModelResponseError) as info:
            transport(_payload(server.base_url))
        message = str(info.value)
        assert "not JSON" in message
        assert KEY not in message
        assert KEY_FRAGMENT not in message


def test_valid_json_without_choices_is_rejected_without_guessing_fields():
    body = json.dumps({"id": "x", "object": "chat.completion", "model": "gpt-4o-mini"})
    with loopback(_raw_response(body)) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        with pytest.raises(model_client.ModelResponseError) as info:
            transport(_payload(server.base_url))
        assert "choices" in str(info.value)


def test_empty_content_is_a_failure_not_an_empty_success():
    with loopback(_content_response("   ")) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        with pytest.raises(model_client.ModelResponseError):
            transport(_payload(server.base_url))


def test_redirect_is_not_followed_and_body_is_not_resent():
    with loopback(_raw_response("nope", delay=0.0)) as target:
        redirect_to = f"{target.base_url}/v1/chat/completions"
        with loopback(
            _raw_response("", status=302, headers={"Location": redirect_to})
        ) as origin:
            transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
            with pytest.raises(model_client.ModelHTTPStatusError) as info:
                transport(_payload(origin.base_url))
            assert info.value.status == 302
            assert len(origin.requests) == 1
            assert target.requests == [], "3xx 绝不能被跟随（否则请求体会被再发一次）"


def test_bad_payload_fails_before_any_request():
    with loopback(_content_response("ok")) as server:
        transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
        with pytest.raises(model_client.ModelEndpointError):
            transport(_payload(server.base_url, model=""))
        with pytest.raises(model_client.ModelEndpointError):
            transport(_payload(server.base_url, messages=[]))
        assert server.requests == []


# ---------------------------------------------------------------------------
# 密钥边界
# ---------------------------------------------------------------------------


def test_key_never_appears_in_any_failure_message():
    """专门一条：所有失败路径的异常文本里都不得出现密钥。"""
    scenarios: list[tuple[str, Responder]] = [
        ("4xx 回显 api_key", _raw_response(json.dumps({"error": KEY}), status=403)),
        ("5xx 回显 Authorization", _raw_response(f"Bearer {KEY}", status=503)),
        ("非 JSON 回显密钥", _raw_response(f"api_key={KEY}", status=200)),
        ("302 且 Location 带密钥", _raw_response(
            "", status=302, headers={"Location": f"https://evil.example.com/{KEY}"}
        )),
    ]
    for label, responder in scenarios:
        with loopback(responder) as server:
            transport = model_client.make_model_transport(api_key=KEY, timeout_ms=2_000)
            with pytest.raises(model_client.ModelClientError) as info:
                transport(_payload(server.base_url))
            exc = info.value
            blob = "\n".join(
                [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
            )
            assert KEY not in blob, f"{label}: 异常文本泄漏了密钥"
            assert KEY_FRAGMENT not in blob, f"{label}: 异常文本泄漏了密钥片段"

    # 发请求之前的失败同样不得泄漏密钥。
    with pytest.raises(model_client.ModelEndpointError) as info:
        model_client.build_chat_completions_url(f"http://public.example.com/?api_key={KEY}")
    assert KEY not in str(info.value)


def test_key_is_absent_from_transport_repr_and_url():
    transport = model_client.make_model_transport(api_key=KEY)
    assert KEY not in repr(transport)
    assert "credential=literal" in repr(transport)
    resolver_transport = model_client.make_model_transport(api_key=lambda provider_id: KEY)
    assert KEY not in repr(resolver_transport)


def test_short_secret_never_echoes_response_body():
    """拿不到可用于字面脱敏的短密钥时，异常里干脆不回显响应体。"""
    short_key = "abc"
    body = json.dumps({"error": f"key was {short_key} and more"})
    with loopback(_raw_response(body, status=502)) as server:
        transport = model_client.make_model_transport(api_key=short_key, timeout_ms=2_000)
        with pytest.raises(model_client.ModelHTTPStatusError) as info:
            transport(_payload(server.base_url))
        assert short_key not in str(info.value)
        assert "key was" not in str(info.value)


def test_redaction_covers_bearer_and_key_value_shapes_without_a_known_secret():
    text = 'Authorization: Bearer abcdef123456, {"api_key": "zzz-999", "note": "keep"}'
    redacted = model_client.redact_secret_text(text)
    assert "abcdef123456" not in redacted
    assert "zzz-999" not in redacted
    assert "keep" in redacted
    assert redacted.count(model_client.REDACTED) == 2


# ---------------------------------------------------------------------------
# 端点校验（纯构造，不发请求）
# ---------------------------------------------------------------------------


def test_url_builder_accepts_https_and_rejects_unsafe_endpoints():
    build = model_client.build_chat_completions_url
    assert build("https://api.example.com") == "https://api.example.com/v1/chat/completions"
    assert build("https://api.example.com/") == "https://api.example.com/v1/chat/completions"
    assert build("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"

    for bad in (
        "http://api.example.com",
        "ftp://api.example.com",
        "https://user:pass@api.example.com",
        "https://api.example.com/v1?api_key=abc",
        "https://api.example.com/v1#frag",
        "",
    ):
        with pytest.raises(model_client.ModelEndpointError):
            build(bad)

    with pytest.raises(model_client.ModelEndpointError) as info:
        build("https://user:pass@api.example.com")
    assert "pass" not in str(info.value), "异常里不得回显内嵌凭据"

    assert build("http://127.0.0.1:9") == "http://127.0.0.1:9/v1/chat/completions"
    assert build("http://localhost:9") == "http://localhost:9/v1/chat/completions"
    with pytest.raises(model_client.ModelEndpointError):
        build("http://127.0.0.1:9", allow_loopback_http=False)


def test_primary_fallback_pair_binds_two_endpoints_without_retrying():
    with loopback(_content_response("primary")) as primary:
        with loopback(_content_response("fallback")) as fallback:
            main, backup = model_client.make_primary_fallback_transports(
                api_key=KEY,
                primary_base_url=primary.base_url,
                fallback_base_url=fallback.base_url,
                timeout_ms=2_000,
            )
            assert main(_payload("https://ignored.example.com")) == "primary"
            assert backup(_payload("https://ignored.example.com")) == "fallback"
            assert len(primary.requests) == 1 and len(fallback.requests) == 1
            assert primary.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"