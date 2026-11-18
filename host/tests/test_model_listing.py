"""模型列表（`GET /v1/models`）的固定测试。

## 为什么这块必须有测试

界面上的「模型 ID」从自由文本框改成了「拉列表后单选」，也就是说
**用户点一下按钮、程序发出一次真实网络请求**。这里最容易出错的是三件事：

1. **URL 归一**：用户可能填 `https://api.openai.com`、带尾斜杠、带 `/v1`、
   甚至直接粘了 `.../v1/chat/completions`。任何形态都必须归一到 `.../v1/models`。
   早先的实现顺序错了，`.../v1/chat/completions` 会被拼成 `/v1/v1/models`。
2. **响应形态**：OpenAI 官方是 `{"data":[{"id":...}]}`，但也有服务商回
   `{"models":[...]}` 或裸数组。取不到必须返回空列表（让界面降级到手填），
   **不能抛异常、不能编造**。
3. **失败要可区分**：401（key 错）、404（服务商不支持该端点）、连接被拒、
   超时，这四种在界面上该给不同提示；统一成一个「失败了」等于把排查负担推给用户。
"""

from __future__ import annotations

import pytest

from astrbot_whale_device.model_client import (
    ModelClientError,
    ModelEndpointError,
    ModelHTTPStatusError,
    ModelRequestError,
    build_models_url,
    parse_models_response,
)

# ---------------------------------------------------------------------------
# URL 归一
# ---------------------------------------------------------------------------


def test_build_models_url_appends_v1_models_to_bare_host() -> None:
    assert build_models_url("https://api.openai.com") == "https://api.openai.com/v1/models"


def test_build_models_url_handles_trailing_slash() -> None:
    assert build_models_url("https://api.openai.com/") == "https://api.openai.com/v1/models"


def test_build_models_url_strips_existing_v1() -> None:
    assert build_models_url("https://api.openai.com/v1") == "https://api.openai.com/v1/models"


def test_build_models_url_strips_existing_chat_completions() -> None:
    """回归：早先顺序错，这里会变成 `/v1/v1/models`（404）。"""
    url = build_models_url("https://api.openai.com/v1/chat/completions")
    assert url == "https://api.openai.com/v1/models"
    assert "/v1/v1/" not in url


def test_build_models_url_strips_existing_models() -> None:
    assert build_models_url("https://api.openai.com/v1/models") == "https://api.openai.com/v1/models"


def test_build_models_url_keeps_custom_path_prefix() -> None:
    # 中转站常带路径前缀（如 `https://example.com/openai`），必须保留。
    url = build_models_url("https://example.com/openai/v1")
    assert url == "https://example.com/openai/v1/models"


def test_build_models_url_accepts_loopback_http() -> None:
    # 本机 Ollama / 反代：回环地址允许明文，仅为本地测试。
    url = build_models_url("http://127.0.0.1:11434")
    assert url == "http://127.0.0.1:11434/v1/models"


def test_build_models_url_rejects_non_loopback_http() -> None:
    with pytest.raises(ModelEndpointError):
        build_models_url("http://api.example.com")


def test_build_models_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ModelEndpointError):
        build_models_url("https://user:pass@api.openai.com")


def test_build_models_url_rejects_query_and_fragment() -> None:
    with pytest.raises(ModelEndpointError):
        build_models_url("https://api.openai.com?x=1")
    with pytest.raises(ModelEndpointError):
        build_models_url("https://api.openai.com#frag")


def test_build_models_url_rejects_empty_and_bad_scheme() -> None:
    with pytest.raises(ModelEndpointError):
        build_models_url("")
    with pytest.raises(ModelEndpointError):
        build_models_url("ftp://api.openai.com")


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def test_parse_openai_official_shape() -> None:
    raw = (
        '{"object":"list","data":['
        '{"id":"gpt-4o-mini","object":"model"},'
        '{"id":"gpt-4o","object":"model"}]}'
    )
    assert parse_models_response(raw) == ["gpt-4o-mini", "gpt-4o"]


def test_parse_deduplicates_and_keeps_order() -> None:
    raw = '{"data":[{"id":"b"},{"id":"a"},{"id":"b"}]}'
    assert parse_models_response(raw) == ["b", "a"]


def test_parse_alternative_models_key() -> None:
    assert parse_models_response('{"models":["llama3","qwen2"]}') == ["llama3", "qwen2"]


def test_parse_bare_array() -> None:
    assert parse_models_response('["a","b"]') == ["a", "b"]


def test_parse_accepts_name_field_too() -> None:
    # 有些服务商用 `name` 而不是 `id`。
    assert parse_models_response('{"data":[{"name":"m1"}]}') == ["m1"]


def test_parse_empty_list_returns_empty() -> None:
    assert parse_models_response('{"data":[]}') == []


def test_parse_garbage_returns_empty_without_raising() -> None:
    """取不到就必须返回空列表（界面降级到手填），**不能抛异常**。"""
    assert parse_models_response("not json") == []
    assert parse_models_response("") == []
    assert parse_models_response("{") == []
    assert parse_models_response("null") == []


def test_parse_skips_non_string_non_dict_entries() -> None:
    assert parse_models_response('{"data":["a", 123, null, {"id":"b"}]}') == ["a", "b"]


# ---------------------------------------------------------------------------
# 失败必须可区分（给界面不同提示的依据）
# ---------------------------------------------------------------------------


def test_error_types_are_distinct_classes() -> None:
    """401 / 404 / 连不上 / 超时 在界面上该给不同提示，所以类型必须能区分。"""
    from astrbot_whale_device.model_client import (
        ModelHttpTransport,
        ModelResponseError,
        ModelTransportTimeout,
    )

    types = {
        ModelRequestError,
        ModelHTTPStatusError,
        ModelResponseError,
        ModelTransportTimeout,
        ModelEndpointError,
        ModelHttpTransport,
    }
    # 全部继承自 ModelClientError（界面统一捕获），但彼此不是同一个类。
    assert len(types) == 6
    assert issubclass(ModelHTTPStatusError, ModelClientError)
    assert issubclass(ModelRequestError, ModelClientError)


def test_http_status_error_carries_status_code() -> None:
    """状态码必须挂在异常上：401 要提示「key 不对」，404 要提示「服务商不支持」。"""
    error = ModelHTTPStatusError("not authorized", status=401)
    assert error.status == 401
