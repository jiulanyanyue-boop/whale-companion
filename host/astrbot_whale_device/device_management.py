"""Phone management shared by the desktop bridge and the AstrBot extension."""

from __future__ import annotations
import base64, hashlib, io, json, os, sqlite3, ssl, threading, time, urllib.request
from dataclasses import replace
from pathlib import Path


class DeviceManagement:
    def __init__(self, host, config_path):
        self.host = host
        self.config_path = Path(config_path).resolve()
        self._pairing = None
        self._lock = threading.RLock()
        self._policy_sync_key = None

    def _sync_policy(self, device_id, connection_key=None):
        """Require a signed phone acknowledgement before reporting permissions saved."""
        with self._lock:
            cfg = self.host.config
            allowed, blocked = (
                sorted(cfg.allowed_packages),
                sorted(cfg.blocked_packages),
            )
            key = (device_id, connection_key, tuple(allowed), tuple(blocked))
            if self._policy_sync_key == key:
                return True
            try:
                response = self.host.request_device(
                    "host.policy.sync",
                    {"allowed_packages": allowed, "blocked_packages": blocked},
                    device_id=device_id,
                )
                evidence = response.evidence or {}
                if (
                    response.status != "ok"
                    or evidence.get("policy_synced") != "true"
                    or sorted(json.loads(evidence.get("allowed_packages", "null")))
                    != allowed
                ):
                    return False
            except Exception:
                return False
            self._policy_sync_key = key
            return True

    def status(self):
        rows = [row.as_dict() for row in self.host.device_server.connections()]
        connected = [r for r in rows if r.get("state") == "ONLINE"]
        synced = False
        if connected:
            row = connected[0]
            synced = self._sync_policy(row["device_id"], row.get("connected_at_ms"))
        else:
            self._policy_sync_key = None
        return {
            "started": self.host.device_server.is_running,
            "connected": bool(connected),
            "device_id": connected[0].get("device_id")
            if connected
            else self.host.config.device_id,
            "connections": rows,
            "allowed_count": len(self.host.config.allowed_packages),
            "policy_synced": synced,
        }

    def pairing_info(self):
        from .pairing import PairingTokenStore
        from .pairing_code import build_presentation, default_code_factory
        from .pairing_endpoint import make_host_identity_from_seed
        from .standalone import phone_facing_urls
        import qrcode

        with self._lock:
            if (
                self._pairing
                and self._pairing["expires_at_ms"] > int(time.time() * 1000) + 30000
            ):
                return self._pairing
            cfg = self.host.config
            server = cfg.device_server
            if not server.uses_tls:
                raise RuntimeError("手机配对需要加密连接。")
            urls = [server.public_url] if server.public_url else phone_facing_urls(
                bind_host=server.host,
                port=int(self.host.device_server.base_port),
                path="/device",
                scheme="wss",
            )
            if not urls:
                raise RuntimeError("尚未找到手机连接地址，请配置公网入口或检查本机网络。")
            secret = self.host._environ.get(cfg.signing_secret_env) or os.environ.get(
                cfg.signing_secret_env, ""
            )
            identity = make_host_identity_from_seed(secret.encode())
            connection = sqlite3.connect(self.config_path.parent / "pairing.sqlite3")
            try:
                record = PairingTokenStore(connection).issue(
                    now_ms=int(time.time() * 1000), code_factory=default_code_factory()
                )
            finally:
                connection.close()
            der = ssl.PEM_cert_to_DER_cert(Path(server.certfile).read_text())
            cert_fingerprint = "" if server.public_url else hashlib.sha256(der).hexdigest()
            presentation = build_presentation(
                record=record,
                host_fingerprint=identity.fingerprint,
                phone_urls=urls,
                cert_fingerprint=cert_fingerprint,
            )
            payload = presentation.qr_payload
            if server.public_url:
                from cryptography import x509
                from cryptography.hazmat.primitives import serialization
                from urllib.parse import quote
                certificate = x509.load_pem_x509_certificate(Path(server.certfile).read_bytes())
                public_key = base64.b64encode(certificate.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
                payload += "&hpk=" + quote(public_key, safe="")
            output = io.BytesIO()
            qrcode.make(payload).save(output, format="PNG")
            self._pairing = {
                "available": True,
                "pairing_code": presentation.pairing_code,
                "expires_at_ms": presentation.expires_at_ms,
                "host_fingerprint": presentation.host_fingerprint,
                "device_url": urls[0],
                "qr_image": "data:image/png;base64,"
                + base64.b64encode(output.getvalue()).decode(),
            }
            return self._pairing

    def connection_settings(self):
        return {"public_url": self.host.config.device_server.public_url}

    def set_connection_settings(self, public_url):
        from .host_config import HostConfig
        if not isinstance(public_url, str):
            raise ValueError("公网地址格式无效")
        with self._lock:
            raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            raw.setdefault("device_server", {})["public_url"] = public_url.strip()
            config = HostConfig.from_mapping(raw)
            temporary = self.config_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.config_path)
            self.host.config = config
            self._pairing = None
            return self.connection_settings()

    def allowed_apps(self):
        state = self.status()
        selected = sorted(self.host.config.allowed_packages)
        if not state["connected"]:
            return {
                "apps": [
                    {"package_name": p, "label": p, "allowed": True} for p in selected
                ],
                "connected": False,
            }
        result = self.host.request_device(
            "device.app.list", {}, device_id=state["device_id"]
        )
        if result.status != "ok":
            raise RuntimeError("手机未能返回应用列表。")
        evidence = result.evidence or {}
        rows = evidence.get("apps")
        if isinstance(rows, str):
            rows = json.loads(rows)
        if rows is None:
            rows = [
                {"package_name": p, "label": p}
                for p in str(evidence.get("packages", "")).splitlines()
                if p.strip()
            ]
        if not isinstance(rows, list):
            raise ValueError("手机返回的应用列表格式无效。")
        apps = [
            {
                "package_name": r["package_name"],
                "label": r.get("label") or r["package_name"],
                "allowed": r["package_name"] in selected,
            }
            for r in rows
            if isinstance(r, dict) and isinstance(r.get("package_name"), str)
        ]
        return {
            "connected": True,
            "apps": sorted(
                apps, key=lambda r: (r["label"].casefold(), r["package_name"])
            ),
        }

    def set_allowed_apps(self, apps):
        from .host_config import HostConfig
        from .security import PackagePolicy

        with self._lock:
            raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            raw["allowed_packages"] = apps
            cfg = HostConfig.from_mapping(raw)
            temporary = self.config_path.with_suffix(".management.tmp")
            temporary.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.config_path)
            self.host.config = cfg
            self.host.plugin.policy = PackagePolicy(
                cfg.allowed_packages, cfg.blocked_packages
            )
            self.host.plugin.router.package_policy = self.host.plugin.policy
            self._policy_sync_key = None
            state = self.status()
            return {
                "apps": sorted(cfg.allowed_packages),
                "synced": state["policy_synced"],
                "message": "应用权限已保存并同步到手机"
                if state["policy_synced"]
                else "电脑已保存，手机尚未确认权限；请更新手机端并重新连接。",
            }


class RemoteDeviceManagement:
    def __init__(self, rpc_url, token):
        self.base = rpc_url.rsplit("/v1/device/", 1)[0]
        self.token = token

    def _request(self, path, payload=None):
        request = urllib.request.Request(
            self.base + "/v1/device/" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        with urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        ).open(request, timeout=12) as response:
            return json.loads(response.read(2 * 1024 * 1024))

    def status(self):
        return self._request("status")

    def pairing_info(self):
        return self._request("pairing")

    def allowed_apps(self):
        return self._request("apps")

    def set_allowed_apps(self, apps):
        return self._request("apps", {"apps": apps})

    def connection_settings(self):
        return self._request("connection")

    def set_connection_settings(self, public_url):
        return self._request("connection", {"public_url": public_url})
