"""Reference-image selection and private TOS upload for Jimeng generation."""
from __future__ import annotations

import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


class ReferenceUploadNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class ReferenceImage:
    character: str
    path: Path
    crop: tuple[int, int, int, int] | None = None

    def bytes(self) -> bytes:
        with Image.open(self.path) as source:
            image = source.convert("RGB")
            if self.crop:
                image = image.crop(self.crop)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()


CHARACTER_CROPS = {"gpt": (13, 43, 183, 220), "claude": (200, 43, 372, 220),
                   "glm": (13, 281, 183, 458), "ds_q": (200, 281, 372, 458)}
SINGLE_REFERENCES = {
    "gpt_q": "Gpt母图【Q版】.png", "claude_q": "Claude母图【Q版】.png",
    "qwen": "千问母图【非Q版】.jpg", "claude": "Claude母图【非Q版】.jpg",
    "deepseek": "deepseek母图【非Q版】.jpg", "gemini": "Gemini母图【非Q版】.jpg",
    "gpt": "Gpt母图【非Q版】.jpg", "grok": "Grok母图【非Q版】.jpg",
    "kimi": "KIMI母图【非Q版】.jpg", "minimax": "MiniMax母图【非Q版】.jpg",
}


def fixed_reference(root: str | Path, character: str) -> ReferenceImage | None:
    key = re.sub(r"[^a-z0-9_]", "", character.lower().replace("-", "_"))
    aliases = {"deepseekq": "ds_q", "dsq": "ds_q", "deepseek_q": "ds_q", "qwen": "qwen", "glm": "glm"}
    key = aliases.get(key, key)
    if key == "normal":
        key = "deepseek"
    if key == "ds_q":
        path = Path(root) / "images" / "AI设定母图【gpt、ds【Q版】、GLM、claude】.webp"
        if not path.is_file():
            raise FileNotFoundError(f"reference image missing for {key}")
        return ReferenceImage(key, path, CHARACTER_CROPS[key])
    if key == "ds":
        key = "deepseek"
    if key in SINGLE_REFERENCES:
        path = Path(root) / "images" / SINGLE_REFERENCES[key]
        if path.is_file():
            return ReferenceImage(key, path)
    if key in {"gpt", "claude", "glm"}:
        path = Path(root) / "images" / "AI设定母图【gpt、ds【Q版】、GLM、claude】.webp"
        if not path.is_file():
            raise FileNotFoundError(f"reference image missing for {key}")
        return ReferenceImage(key, path, CHARACTER_CROPS[key])
    if key not in CHARACTER_CROPS:
        return None
    return None


class TOSReferenceUploader:
    """Upload privately and return a short-lived signed GET URL."""
    def __init__(self, *, ak: str | None = None, sk: str | None = None,
                 endpoint: str | None = None, region: str | None = None,
                 bucket: str | None = None, expires: int = 300,
                 client_factory: Any = None):
        self.ak = ak or os.getenv("TOS_ACCESS_KEY", os.getenv("VOLCENGINE_ACCESS_KEY", "")).strip()
        self.sk = sk or os.getenv("TOS_SECRET_KEY", os.getenv("VOLCENGINE_SECRET_KEY", "")).strip()
        self.endpoint = endpoint or os.getenv("TOS_ENDPOINT", "").strip()
        self.region = region or os.getenv("TOS_REGION", "").strip()
        self.bucket = bucket or os.getenv("TOS_BUCKET", "").strip()
        self.expires = max(60, min(int(expires), 900))
        self.client_factory = client_factory

    def upload(self, image: ReferenceImage) -> str:
        if not all((self.ak, self.sk, self.endpoint, self.region, self.bucket)):
            raise ReferenceUploadNotConfigured("reference_upload_not_configured")
        try:
            import tos
            factory = self.client_factory or tos.TosClientV2
            client = factory(self.ak, self.sk, self.endpoint, self.region)
            key = f"astrbot-whale-references/{uuid.uuid4().hex}.png"
            content = image.bytes()
            client.put_object(self.bucket, key, content_length=len(content), content_type="image/png", content=content)
            return client.pre_signed_url(tos.HttpMethodType.Http_Method_Get, bucket=self.bucket, key=key, expires=self.expires).signed_url
        except ReferenceUploadNotConfigured:
            raise
        except Exception as exc:
            raise RuntimeError("reference_upload_failed") from exc


class HostedReferenceUploader:
    """Upload to the user's HTTPS image store and receive a short-lived URL."""

    def upload(self, image: ReferenceImage) -> str:
        import json
        from urllib.request import Request, HTTPRedirectHandler, build_opener
        from urllib.parse import urlsplit
        url = os.environ.get("WHALE_REFERENCE_UPLOAD_URL", "")
        token = os.environ.get("WHALE_REFERENCE_UPLOAD_TOKEN", "")
        if urlsplit(url).scheme != "https" or not token:
            raise ReferenceUploadNotConfigured("reference_upload_not_configured")
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = Request(url, data=image.bytes(), headers={"Authorization": "Bearer " + token, "Content-Type": "image/png", "User-Agent": "WhaleReference/1.0"})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.loads(response.read(8192))
        hosted = result.get("url", "")
        if urlsplit(hosted).scheme != "https" or urlsplit(hosted).netloc != urlsplit(url).netloc:
            raise RuntimeError("invalid_hosted_reference_url")
        return hosted
