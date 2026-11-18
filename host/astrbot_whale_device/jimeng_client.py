"""Official Jimeng 4.0 Volcengine Visual API client (credentials from env only)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import base64
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

ENDPOINT = "https://visual.volcengineapi.com/"
SERVICE = "cv"
REGION = "cn-north-1"
VERSION = "2022-08-31"
REQ_KEY = "jimeng_t2i_v40"


class JimengError(RuntimeError):
    pass


class CredentialsMissing(JimengError):
    pass


@dataclass(frozen=True)
class Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Credentials":
        ak, sk = os.environ.get("VOLCENGINE_ACCESS_KEY", "").strip(), os.environ.get("VOLCENGINE_SECRET_KEY", "").strip()
        if not ak or not sk:
            raise CredentialsMissing("即梦生成未启用：请配置 VOLCENGINE_ACCESS_KEY 和 VOLCENGINE_SECRET_KEY。")
        return cls(ak, sk, os.environ.get("VOLCENGINE_SESSION_TOKEN", "").strip())


def _sign_headers(body: bytes, credentials: Credentials, now: datetime, *, action: str) -> dict[str, str]:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    content_type = "application/json"
    payload_hash = hashlib.sha256(body).hexdigest()
    header_values = {"content-type": content_type, "host": "visual.volcengineapi.com",
                     "x-content-sha256": payload_hash, "x-date": stamp}
    if credentials.session_token:
        header_values["x-security-token"] = credentials.session_token
    signed_headers = ";".join(sorted(header_values))
    canonical_headers = "".join(f"{name}:{header_values[name]}\n" for name in sorted(header_values))
    canonical_query = urlencode(sorted({"Action": action, "Version": VERSION}.items()))
    canonical = "\n".join(("POST", "/", canonical_query, canonical_headers, signed_headers, payload_hash))
    scope = f"{day}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join(("HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = hmac.new(credentials.secret_key.encode(), day.encode(), hashlib.sha256).digest()
    for part in (REGION, SERVICE, "request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = {"Content-Type": content_type, "Host": "visual.volcengineapi.com", "X-Content-Sha256": payload_hash, "X-Date": stamp,
              "Authorization": f"HMAC-SHA256 Credential={credentials.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"}
    if credentials.session_token:
        result["X-Security-Token"] = credentials.session_token
    return result


def _default_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = Request(url, data=body, headers=headers, method="POST")
    with build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        content = response.read(36 * 1024 * 1024 + 1)
        if len(content) > 36 * 1024 * 1024:
            raise JimengError("image response too large")
        return content


class JimengClient:
    """One-shot submission (never retried) and bounded task polling."""
    def __init__(self, credentials: Credentials | None = None, transport: Callable[[str, dict[str, str], bytes, float], bytes] | None = None,
                 *, timeout: float = 20.0, max_polls: int = 8, poll_interval: float = 2.0):
        self.credentials = credentials or Credentials.from_env()
        self.transport = transport or _default_transport
        self.timeout, self.max_polls, self.poll_interval = timeout, max(1, min(max_polls, 20)), max(0.0, min(poll_interval, 10.0))

    def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        # Signer canonical query is sorted by key as in the official signer.
        url = ENDPOINT + "?" + urlencode(sorted({"Action": action, "Version": VERSION}.items()))
        headers = _sign_headers(body, self.credentials, datetime.now(timezone.utc), action=action)
        try:
            raw = self.transport(url, headers, body, self.timeout)
            result = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception as exc:
            # Error bodies may echo credentials or request content; never expose them.
            raise JimengError(f"即梦请求失败：{type(exc).__name__}") from None
        if not isinstance(result, dict):
            raise JimengError("即梦返回格式无效")
        return result

    def submit(self, prompt: str, *, verified_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt cannot be empty")
        if len(prompt) > 800:
            raise ValueError("Jimeng 4.0 prompt must be at most 800 characters")
        payload = dict(verified_fields or {})
        allowed = {"image_urls", "size", "width", "height", "scale", "force_single", "min_ratio", "max_ratio"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unsupported Jimeng 4.0 request fields: {', '.join(sorted(unknown))}")
        if "size" in payload and ({"width", "height"} & payload.keys()):
            raise ValueError("Jimeng 4.0 size and width/height are mutually exclusive")
        if ("width" in payload) != ("height" in payload):
            raise ValueError("Jimeng 4.0 width and height must be supplied together")
        if "req_key" in payload and payload["req_key"] != REQ_KEY:
            raise ValueError("only Jimeng 4.0 req_key is supported")
        payload["req_key"] = REQ_KEY
        payload["prompt"] = prompt
        payload["force_single"] = True
        if "image_urls" in payload:
            urls = payload["image_urls"]
            if not isinstance(urls, list) or len(urls) != 1 or not isinstance(urls[0], str) or not urls[0].strip():
                raise ValueError("Jimeng 4.0 reference generation requires exactly one image URL")
        # A submit request is intentionally never retried: timeout may mean it was billed.
        return self._call("CVSync2AsyncSubmitTask", payload)

    def get_result(self, task_id: str, *, verified_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(verified_fields or {})
        if set(payload) - {"req_json"}:
            raise ValueError("only documented req_json is accepted for result queries")
        payload["req_key"] = REQ_KEY
        payload["task_id"] = task_id
        return self._call("CVSync2AsyncGetResult", payload)

    def wait_result(self, task_id: str, *, verified_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self.max_polls):
            result = self.get_result(task_id, verified_fields=verified_fields)
            code = result.get("code")
            data = result.get("data")
            if code is not None and code != 10000:
                raise JimengError(f"即梦任务查询失败（code={code}）")
            status = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
            if status in {"done", "not_found", "expired"}:
                return result
            if attempt + 1 < self.max_polls:
                time.sleep(self.poll_interval)
        raise JimengError("任务仍在处理中，达到查询次数上限")


def image_bytes_from_result(result: dict[str, Any]) -> list[bytes]:
    """Decode the documented binary_data_base64 result; URL fetching is left to an explicit downloader."""
    data = result.get("data")
    if not isinstance(data, dict) or str(data.get("status", "")).lower() != "done":
        raise JimengError("即梦任务尚未成功完成")
    if result.get("code", 10000) != 10000:
        raise JimengError(f"即梦任务失败（code={result.get('code')}）")
    values = data.get("binary_data_base64")
    if not values:
        raise JimengError("结果没有图片二进制内容；请启用官方 req_json.return_url 后通过受限下载器取回")
    decoded = []
    for value in values:
        if not isinstance(value, str):
            raise JimengError("即梦图片结果格式无效")
        try:
            content = base64.b64decode(value, validate=True)
        except Exception:
            raise JimengError("即梦图片编码无效") from None
        if not content or len(content) > 25 * 1024 * 1024:
            raise JimengError("即梦图片为空或超过本地缓存上限")
        decoded.append(content)
    return decoded
