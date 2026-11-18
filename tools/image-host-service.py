"""Small authenticated, expiring reference-image store behind Caddy."""

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ.get("WHALE_IMAGE_ROOT", "/var/lib/whale-images"))
TOKEN = (
    Path(os.environ.get("WHALE_IMAGE_TOKEN_FILE", "/etc/whale-images/token"))
    .read_text()
    .strip()
)
BASE = os.environ["WHALE_IMAGE_BASE"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, code, body, mime="application/json"):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/whale/media":
            return self.reply(404, {})
        if not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + TOKEN
        ):
            return self.reply(401, {"error": "unauthorized"})
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 4 * 1024 * 1024:
                return self.reply(413, {"error": "image_too_large"})
            self.connection.settimeout(15)
            image = self.rfile.read(size)
            if len(image) != size or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                return self.reply(400, {"error": "png_required"})
            for old in ROOT.glob("*.png"):
                if old.stat().st_mtime < time.time() - 1800:
                    old.unlink(missing_ok=True)
            name = secrets.token_hex(24) + ".png"
            (ROOT / name).write_bytes(image)
            expires = int(time.time()) + 900
            signature = hmac.new(
                TOKEN.encode(), f"{name}:{expires}".encode(), hashlib.sha256
            ).hexdigest()
            return self.reply(
                200, {"url": f"{BASE}/{name}?expires={expires}&sig={signature}"}
            )
        except (OSError, ValueError):
            return self.reply(400, {"error": "upload_failed"})

    def do_GET(self):
        url = urlsplit(self.path)
        name = url.path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[a-f0-9]{48}\.png", name):
            return self.reply(404, {})
        query = parse_qs(url.query)
        try:
            expires = int(query["expires"][0])
            signature = query["sig"][0]
        except (KeyError, ValueError, IndexError):
            return self.reply(403, {})
        expected = hmac.new(
            TOKEN.encode(), f"{name}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        if (
            expires < time.time()
            or expires > time.time() + 901
            or not hmac.compare_digest(expected, signature)
        ):
            return self.reply(403, {})
        try:
            return self.reply(200, (ROOT / name).read_bytes(), "image/png")
        except OSError:
            return self.reply(404, {})


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", 8089), Handler).serve_forever()
