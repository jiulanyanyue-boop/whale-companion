"""M05 主机侧配对与身份（Python）。

规范来源：``docs/modules/M05-pairing-transport-product.md`` 与
``M05-pairing-transport-development.md``。

本模块与 Android 侧 ``KeystoreIdentity.kt`` / ``PairingController.kt`` 是**同一套规则的两侧实现**：

- canonical JSON 与 :func:`astrbot_whale_device.security.canonical_payload` **字节级一致**
  （``ensure_ascii=False, sort_keys=True, separators=(",", ":")``，去掉顶层 ``signature``）。
  这里直接**复用** ``security.canonical_payload``，不写第二份——两份实现是漂移的源头。
- 设备指纹 = ``sha256(公钥编码字节)`` 的小写十六进制，与 Kotlin ``Fingerprints.of`` 同规则。
- 主机身份也固定指纹；指纹变化 → ``PAIRING_REQUIRED``，**绝不自动接受**。
- 配对完成后一次性 token **立即失效**。
- 主机只保存 ``device_id``、公钥、指纹、能力版本与连接配置；**不保存任何私钥**。

禁止项对应：
- 只校验 ``device_id`` 不校验公钥指纹 → :func:`verify_envelope_signature` 必须同时过指纹与签名。
- 内存 Map 作为唯一幂等/计数器存储 → token 与设备表都走 SQLite（``pairing_state`` 表）。
- 自动信任身份指纹变化 → :meth:`PairingService.bind_device` 遇到指纹变化直接置 ``PAIRING_REQUIRED``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .security import canonical_payload


# ---------------------------------------------------------------------------
# 固定常量（与 Android 侧值守）
# ---------------------------------------------------------------------------

#: 配对 token 默认有效期：短期（10 分钟）。
PAIRING_TOKEN_TTL_MS = 10 * 60 * 1000

#: 指纹长度：SHA-256 = 64 个小写十六进制字符。
FINGERPRINT_HEX_LENGTH = 64

#: 配对六步的固定顺序（与 Kotlin ``PairingStep`` 一一对应）。
PAIRING_STEPS: tuple[str, ...] = (
    "host-issued-token",
    "device-entered-code",
    "device-generated-identity",
    "host-saved-identity",
    "user-confirmed-fingerprint",
    "token-revoked",
)

#: 连接状态机（与 Kotlin ``PairingState`` / ``ConnectionState`` 对齐）。
PAIRING_STATE_UNPAIRED = "UNPAIRED"
PAIRING_STATE_PAIRING = "PAIRING"
PAIRING_STATE_PAIRED_OFFLINE = "PAIRED_OFFLINE"
PAIRING_STATE_PAIRING_REQUIRED = "PAIRING_REQUIRED"
PAIRING_STATE_REVOKED = "REVOKED"

PAIRING_STATES: frozenset[str] = frozenset({
    PAIRING_STATE_UNPAIRED,
    PAIRING_STATE_PAIRING,
    PAIRING_STATE_PAIRED_OFFLINE,
    PAIRING_STATE_PAIRING_REQUIRED,
    PAIRING_STATE_REVOKED,
})

#: 退避固定序列（秒），最大 30。
BACKOFF_DELAYS_SECONDS: tuple[int, ...] = (1, 2, 4, 8, 16, 30)
MAX_BACKOFF_SECONDS = 30


class PairingError(ValueError):
    """配对流程拒绝。``code`` 只能是已登记的 CommandErrors 之一。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.reason = message
        super().__init__(f"{self.code}: {message}")


# ---------------------------------------------------------------------------
# canonical JSON 与指纹（与 Kotlin 逐字一致）
# ---------------------------------------------------------------------------


def canonical_text(fields: Mapping[str, Any]) -> str:
    """去掉顶层 ``signature`` 后的 canonical JSON 文本（UTF-8 编码前）。"""
    unsigned = {key: value for key, value in fields.items() if key != "signature"}
    return canonical_payload(unsigned).decode("utf-8")


def canonical_bytes(fields: Mapping[str, Any]) -> bytes:
    """签名输入字节。**必须**与 Kotlin ``CanonicalJson.encodeUnsignedBytes`` 相同。"""
    return canonical_payload({key: value for key, value in fields.items() if key != "signature"})


def fingerprint_of(public_key_bytes: bytes) -> str:
    """``sha256(公钥编码字节)`` 的小写十六进制，与 Kotlin ``Fingerprints.of`` 同规则。"""
    return hashlib.sha256(bytes(public_key_bytes)).hexdigest()


def public_key_bytes_of(public_key_encoded: str) -> bytes:
    """把手上的「公钥文本」还原成**原始编码字节**，再拿去算指纹。

    为什么必须有这一步：设备端 Kotlin 的 ``Fingerprints.of(publicKeyBytes)``
    是对**公钥原始字节**（X.509 DER）求 SHA-256；而主机这边收到的是
    **Base64 文本**（`PublicIdentity.publicKeyEncoded` 就是 Base64 串）。

    如果直接把 Base64 文本的 UTF-8 字节拿去 sha256，两端算出来的指纹
    必然不同——实测就是这样，主机永远报
    "declared fingerprint does not match the public key"，配对走不下去。

    所以这里先 Base64 解码回到原始字节，再算指纹。

    兼容：若传入的不是合法 Base64（例如调用方直接给了十六进制或裸 DER 字节的
    文本形式），退回「按 UTF-8 字节处理」的旧行为，避免把历史数据判成非法。
    """
    raw = (public_key_encoded or "").strip()
    if not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        # `validate=True` 会拒绝含空白/换行的 Base64；再试一次宽松解码。
        try:
            return base64.b64decode(raw)
        except Exception:
            return raw.encode("utf-8")


def fingerprint_of_encoded(public_key_encoded: str) -> str:
    """从 Base64 公钥文本算指纹（主机侧**唯一**应采用的口径）。"""
    return fingerprint_of(public_key_bytes_of(public_key_encoded))


def fingerprint_matches(expected: str, actual: str) -> bool:
    """恒定时间比较，避免时序差异。"""
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    if len(expected) != len(actual):
        return False
    return hmac.compare_digest(expected, actual)


def is_well_formed_fingerprint(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != FINGERPRINT_HEX_LENGTH:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def backoff_delay_seconds(attempt: int) -> int:
    """固定退避序列：1、2、4、8、16、30，之后恒为 30。"""
    if attempt <= 0:
        return BACKOFF_DELAYS_SECONDS[0]
    index = min(attempt - 1, len(BACKOFF_DELAYS_SECONDS) - 1)
    return BACKOFF_DELAYS_SECONDS[index]


def backoff_delay_ms(attempt: int) -> int:
    return backoff_delay_seconds(attempt) * 1000


# ---------------------------------------------------------------------------
# 主机身份
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostIdentity:
    """主机身份：公钥 + 固定指纹。**不含私钥**（私钥不进这个类型）。"""

    host_id: str
    public_key_encoded: str
    fingerprint: str

    @property
    def has_well_formed_fingerprint(self) -> bool:
        return is_well_formed_fingerprint(self.fingerprint)


def host_identity_from_public_key(host_id: str, public_key_bytes: bytes) -> HostIdentity:
    """用公钥字节构造主机身份（指纹由公钥算出，不接受外部传入）。"""
    encoded = bytes(public_key_bytes)
    return HostIdentity(
        host_id=host_id,
        public_key_encoded=encoded.decode("utf-8"),
        fingerprint=fingerprint_of(encoded),
    )


# ---------------------------------------------------------------------------
# 一次性配对 token
# ---------------------------------------------------------------------------


@dataclass
class PairingTokenRecord:
    """落库形态的一次性 token。``used`` 一旦为真就永不复原。"""

    token: str
    pairing_code: str
    expires_at_ms: int
    used: bool = False
    revoked: bool = False


class PairingTokenStore:
    """SQLite 支撑的一次性 token（**不用内存 Map 作为唯一真相**）。

    ``pairing_state`` 表由本模块 ``CREATE TABLE IF NOT EXISTS`` 建在当前连接上；
    它不属于 M12 的固定八张表，因此**独立命名**，不去污染 ``schedules`` / ``effects``。
    """

    TABLE = "pairing_state"

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.RLock()
        with self._lock:
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                "token TEXT PRIMARY KEY, "
                "pairing_code TEXT NOT NULL, "
                "expires_at_ms INTEGER NOT NULL, "
                "used INTEGER NOT NULL DEFAULT 0, "
                "revoked INTEGER NOT NULL DEFAULT 0)"
            )
            self.db.commit()

    def issue(
        self,
        now_ms: int,
        ttl_ms: int = PAIRING_TOKEN_TTL_MS,
        *,
        code_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> PairingTokenRecord:
        """第 1 步：生成一次性配对码 + 短期 token。"""
        token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
        code = (code_factory or (lambda: f"{secrets.randbelow(1_000_000):06d}"))()
        expires = int(now_ms) + max(0, int(ttl_ms))
        with self._lock:
            self.db.execute(
                f"INSERT INTO {self.TABLE}(token,pairing_code,expires_at_ms,used,revoked) VALUES(?,?,?,0,0)",
                (token, code, expires),
            )
            self.db.commit()
        return PairingTokenRecord(token=token, pairing_code=code, expires_at_ms=expires)

    def get(self, token: str) -> PairingTokenRecord | None:
        with self._lock:
            row = self.db.execute(
                f"SELECT token,pairing_code,expires_at_ms,used,revoked FROM {self.TABLE} WHERE token=?",
                (str(token),),
            ).fetchone()
        if row is None:
            return None
        return PairingTokenRecord(
            token=str(row[0]),
            pairing_code=str(row[1]),
            expires_at_ms=int(row[2]),
            used=bool(row[3]),
            revoked=bool(row[4]),
        )

    def consume(self, token: str, now_ms: int) -> PairingTokenRecord:
        """第 2 步：消耗 token。过期 / 已用过 / 已吊销一律拒绝。

        用条件 UPDATE 保证**并发下也只有一次成功**（``used=0 AND revoked=0``）。
        """
        with self._lock:
            record = self.get(token)
            if record is None:
                raise PairingError("SIGNATURE_REQUIRED", "unknown pairing token")
            if record.revoked or record.used:
                raise PairingError("SIGNATURE_REQUIRED", "pairing token was already used")
            if record.expires_at_ms > 0 and int(now_ms) >= record.expires_at_ms:
                raise PairingError("EXPIRED", "pairing token expired")
            cursor = self.db.execute(
                f"UPDATE {self.TABLE} SET used=1 WHERE token=? AND used=0 AND revoked=0",
                (str(token),),
            )
            self.db.commit()
            if int(cursor.rowcount) != 1:
                raise PairingError("SIGNATURE_REQUIRED", "pairing token was already used")
        return PairingTokenRecord(
            token=record.token,
            pairing_code=record.pairing_code,
            expires_at_ms=record.expires_at_ms,
            used=True,
        )

    def revoke(self, token: str) -> None:
        """第 6 步：配对完成后一次性 token **立即失效**。"""
        with self._lock:
            self.db.execute(
                f"UPDATE {self.TABLE} SET revoked=1, used=1 WHERE token=?",
                (str(token),),
            )
            self.db.commit()

    def is_usable(self, token: str, now_ms: int) -> bool:
        record = self.get(token)
        if record is None or record.used or record.revoked:
            return False
        return record.expires_at_ms <= 0 or int(now_ms) < record.expires_at_ms


# ---------------------------------------------------------------------------
# 设备表（主机只保存 device_id / 公钥 / 指纹 / 能力版本 / 连接配置）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    public_key_encoded: str
    fingerprint: str
    capabilities_version: str
    connection_config: str
    display_name: str
    state: str

    @property
    def has_well_formed_fingerprint(self) -> bool:
        return is_well_formed_fingerprint(self.fingerprint)


class PairedDeviceStore:
    """已配对设备表。**只有公钥，没有私钥字段**。"""

    TABLE = "paired_devices"

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.RLock()
        with self._lock:
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                "device_id TEXT PRIMARY KEY, "
                "public_key TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, "
                "capabilities_version TEXT NOT NULL DEFAULT '', "
                "connection_config TEXT NOT NULL DEFAULT '', "
                "display_name TEXT NOT NULL DEFAULT '', "
                "state TEXT NOT NULL)"
            )
            self.db.commit()

    def get(self, device_id: str) -> DeviceRecord | None:
        with self._lock:
            row = self.db.execute(
                f"SELECT device_id,public_key,fingerprint,capabilities_version,connection_config,"
                f"display_name,state FROM {self.TABLE} WHERE device_id=?",
                (str(device_id),),
            ).fetchone()
        if row is None:
            return None
        return DeviceRecord(
            device_id=str(row[0]),
            public_key_encoded=str(row[1]),
            fingerprint=str(row[2]),
            capabilities_version=str(row[3]),
            connection_config=str(row[4]),
            display_name=str(row[5]),
            state=str(row[6]),
        )

    def save(self, record: DeviceRecord) -> None:
        with self._lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO {self.TABLE}"
                "(device_id,public_key,fingerprint,capabilities_version,connection_config,display_name,state)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    record.device_id,
                    record.public_key_encoded,
                    record.fingerprint,
                    record.capabilities_version,
                    record.connection_config,
                    record.display_name,
                    record.state,
                ),
            )
            self.db.commit()

    def set_state(self, device_id: str, state: str) -> None:
        if state not in PAIRING_STATES:
            raise PairingError("INVALID_PAYLOAD", f"unknown pairing state: {state!r}")
        with self._lock:
            self.db.execute(
                f"UPDATE {self.TABLE} SET state=? WHERE device_id=?",
                (state, str(device_id)),
            )
            self.db.commit()

    def delete(self, device_id: str) -> None:
        with self._lock:
            self.db.execute(f"DELETE FROM {self.TABLE} WHERE device_id=?", (str(device_id),))
            self.db.commit()


# ---------------------------------------------------------------------------
# 配对服务：六步流程
# ---------------------------------------------------------------------------


@dataclass
class PairingSession:
    """一次配对会话的进度。``step`` 取值见 :data:`PAIRING_STEPS`。"""

    device_id: str
    token: str
    step: str = PAIRING_STEPS[0]
    device: DeviceRecord | None = None
    confirmed: bool = False
    history: list[str] = field(default_factory=list)


class PairingService:
    """M05 主机侧配对流程（六步，顺序固定）。

    六步落地位置：

    1. :meth:`issue_token` —— 生成配对码 + 短期 token
    2. :meth:`submit_code` —— 设备回传配对码，消耗 token
    3. :meth:`register_device_identity` —— 保存 device_id / 公钥 / 指纹
    4. :meth:`save_device` —— 保存能力版本与连接配置（第 4 步正式完成）
    5. :meth:`confirm_fingerprint` —— 双方显示后用户确认
    6. :meth:`finalize` —— token 立即失效
    """

    def __init__(
        self,
        db: sqlite3.Connection | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.db = db or sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.RLock()
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.tokens = PairingTokenStore(self.db)
        self.devices = PairedDeviceStore(self.db)
        self.host_identity: HostIdentity | None = None

    # -- 主机身份 --------------------------------------------------------
    def set_host_identity(self, identity: HostIdentity) -> None:
        """固定主机身份。指纹形状不合规直接拒绝。"""
        if not identity.has_well_formed_fingerprint:
            raise PairingError("INVALID_PAYLOAD", "host fingerprint must be a sha-256 hex digest")
        self.host_identity = identity

    def host_fingerprint(self) -> str:
        if self.host_identity is None:
            raise PairingError("SIGNATURE_REQUIRED", "host identity is not configured")
        return self.host_identity.fingerprint

    def assert_host_fingerprint(self, observed: str) -> None:
        """主机指纹必须与固定值逐字相同；变化 → 人工重新配对（绝不自动接受）。"""
        expected = self.host_fingerprint()
        if not fingerprint_matches(expected, str(observed).strip().lower()):
            raise PairingError("SIGNATURE_REQUIRED", "host fingerprint changed; manual re-pairing is required")

    # -- 第 1 步 ---------------------------------------------------------
    def issue_token(
        self,
        now_ms: int | None = None,
        ttl_ms: int = PAIRING_TOKEN_TTL_MS,
        *,
        code_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> PairingTokenRecord:
        stamp = self.clock() if now_ms is None else int(now_ms)
        return self.tokens.issue(
            stamp,
            ttl_ms,
            code_factory=code_factory,
            token_factory=token_factory,
        )

    # -- 第 2 步 ---------------------------------------------------------
    def submit_code(self, token: str, pairing_code: str, now_ms: int | None = None) -> PairingTokenRecord:
        stamp = self.clock() if now_ms is None else int(now_ms)
        record = self.tokens.get(token)
        if record is None:
            raise PairingError("SIGNATURE_REQUIRED", "unknown pairing token")
        if not hmac.compare_digest(record.pairing_code, str(pairing_code)):
            raise PairingError("SIGNATURE_REQUIRED", "pairing code does not match the issued token")
        return self.tokens.consume(token, stamp)

    # -- 第 3 步 ---------------------------------------------------------
    def register_device_identity(
        self,
        device_id: str,
        public_key_encoded: str,
        *,
        fingerprint: str | None = None,
        now_ms: int | None = None,
    ) -> DeviceRecord:
        """保存设备公钥与指纹。

        指纹**必须**由公钥算出（或与算出的值逐字一致）：只信外部传进来的
        ``fingerprint`` 等于「只校验 device_id 不校验公钥」，是禁止项。
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise PairingError("INVALID_PAYLOAD", "device_id is required")
        if not isinstance(public_key_encoded, str) or not public_key_encoded.strip():
            raise PairingError("INVALID_PAYLOAD", "device public key is required")
        computed = fingerprint_of_encoded(public_key_encoded)
        if fingerprint is not None and not fingerprint_matches(computed, str(fingerprint).strip().lower()):
            raise PairingError("SIGNATURE_REQUIRED", "declared fingerprint does not match the public key")
        existing = self.devices.get(device_id)
        if existing is not None and not fingerprint_matches(existing.fingerprint, computed):
            # 设备公钥变化 = 设备身份变化：必须人工重新配对，不得静默覆盖。
            self.devices.set_state(device_id, PAIRING_STATE_PAIRING_REQUIRED)
            raise PairingError("SIGNATURE_REQUIRED", "device fingerprint changed; manual re-pairing is required")
        record = DeviceRecord(
            device_id=device_id,
            public_key_encoded=public_key_encoded,
            fingerprint=computed,
            capabilities_version=existing.capabilities_version if existing else "",
            connection_config=existing.connection_config if existing else "",
            display_name=existing.display_name if existing else "",
            state=existing.state if existing else PAIRING_STATE_PAIRING,
        )
        self.devices.save(record)
        del now_ms
        return record

    # -- 第 4 步 ---------------------------------------------------------
    def save_device(
        self,
        device_id: str,
        *,
        capabilities_version: str = "",
        connection_config: str = "",
        display_name: str = "",
    ) -> DeviceRecord:
        record = self.devices.get(device_id)
        if record is None:
            raise PairingError("INVALID_PAYLOAD", "device identity was not registered")
        if not record.has_well_formed_fingerprint:
            raise PairingError("INVALID_PAYLOAD", "device fingerprint is malformed")
        updated = DeviceRecord(
            device_id=record.device_id,
            public_key_encoded=record.public_key_encoded,
            fingerprint=record.fingerprint,
            capabilities_version=capabilities_version,
            connection_config=connection_config,
            display_name=display_name,
            state=PAIRING_STATE_PAIRING,
        )
        self.devices.save(updated)
        return updated

    # -- 第 5 步 ---------------------------------------------------------
    def confirm_fingerprint(
        self,
        device_id: str,
        *,
        user_accepted: bool,
        observed_host_fingerprint: str,
    ) -> DeviceRecord:
        """用户确认双方指纹后才启用。

        未确认 → 保持 ``PAIRING`` 并要求重新配对；**绝不因为"签名能验过"就启用**。
        """
        record = self.devices.get(device_id)
        if record is None:
            raise PairingError("INVALID_PAYLOAD", "device identity was not registered")
        if not record.has_well_formed_fingerprint:
            raise PairingError("INVALID_PAYLOAD", "device fingerprint is malformed")
        # 主机身份也必须核对：确认页展示的就是这两个指纹。
        self.assert_host_fingerprint(observed_host_fingerprint)
        if not user_accepted:
            self.devices.set_state(device_id, PAIRING_STATE_PAIRING_REQUIRED)
            raise PairingError("ACTION_REJECTED", "fingerprint was not confirmed by the user")
        updated = DeviceRecord(
            device_id=record.device_id,
            public_key_encoded=record.public_key_encoded,
            fingerprint=record.fingerprint,
            capabilities_version=record.capabilities_version,
            connection_config=record.connection_config,
            display_name=record.display_name,
            state=PAIRING_STATE_PAIRED_OFFLINE,
        )
        self.devices.save(updated)
        return updated

    # -- 第 6 步 ---------------------------------------------------------
    def finalize(self, token: str) -> None:
        """配对完成：一次性 token **立即失效**。

        强制吊销（即使未被消耗），保证"立即失效"是不可逆的。
        """
        self.tokens.revoke(token)
        if self.tokens.is_usable(token, self.clock()):
            raise PairingError("SIGNATURE_REQUIRED", "pairing token was not revoked")

    # -- 吊销 ------------------------------------------------------------
    def revoke_device(self, device_id: str) -> None:
        """任何状态 → ``REVOKED``。"""
        if self.devices.get(device_id) is None:
            raise PairingError("INVALID_PAYLOAD", "unknown device")
        self.devices.set_state(device_id, PAIRING_STATE_REVOKED)

    # -- 断线与退避 ------------------------------------------------------
    def on_disconnect(self, device_id: str, attempt: int) -> int:
        """断线退避毫秒数。状态只落库，不依赖进程内计时器。"""
        if self.devices.get(device_id) is None:
            raise PairingError("INVALID_PAYLOAD", "unknown device")
        return backoff_delay_ms(attempt)

    def on_heartbeat(self, device_id: str) -> None:
        """收到 heartbeat：退避清零（下一轮从 1 秒重新开始）。"""
        record = self.devices.get(device_id)
        if record is None:
            raise PairingError("INVALID_PAYLOAD", "unknown device")
        if record.state == PAIRING_STATE_REVOKED:
            raise PairingError("SIGNATURE_REQUIRED", "device is revoked")
        self.devices.set_state(device_id, PAIRING_STATE_PAIRED_OFFLINE)


# ---------------------------------------------------------------------------
# 信封签名校验（主机视角）
# ---------------------------------------------------------------------------


def verify_envelope_signature(
    fields: Mapping[str, Any],
    *,
    device_public_key_encoded: str,
    expected_device_fingerprint: str,
    expected_host_fingerprint: str,
    observed_host_fingerprint: str,
    verifier: Callable[[str, bytes, str], bool],
) -> None:
    """校验一个信封的签名。

    判定顺序固定（与 Kotlin ``SignatureGate.check`` 一致）：

    1. `signature` 为空 → ``SIGNATURE_REQUIRED``
    2. 设备公钥与已保存指纹不符 → ``SIGNATURE_REQUIRED``
       （禁止项：只校验 device_id 不校验公钥指纹）
    3. 主机指纹变化 → ``SIGNATURE_REQUIRED``（状态应转 ``PAIRING_REQUIRED``）
    4. 密码学校验失败 → ``SIGNATURE_REQUIRED``
    """
    signature = fields.get("signature")
    if not isinstance(signature, str) or not signature.strip():
        raise PairingError("SIGNATURE_REQUIRED", "request signature is required")

    computed = fingerprint_of_encoded(str(device_public_key_encoded))
    if not fingerprint_matches(str(expected_device_fingerprint), computed):
        raise PairingError("SIGNATURE_REQUIRED", "device public key does not match the pinned fingerprint")

    if not fingerprint_matches(str(expected_host_fingerprint).strip().lower(), str(observed_host_fingerprint).strip().lower()):
        raise PairingError("SIGNATURE_REQUIRED", "host fingerprint changed; manual re-pairing is required")

    payload = canonical_bytes(fields)
    if not verifier(device_public_key_encoded, payload, signature):
        raise PairingError("SIGNATURE_REQUIRED", "envelope signature verification failed")


def pairing_states_are_fixed() -> bool:
    """自检：五态齐全、退避序列固定、六步齐全。"""
    if PAIRING_STATES != {
        PAIRING_STATE_UNPAIRED,
        PAIRING_STATE_PAIRING,
        PAIRING_STATE_PAIRED_OFFLINE,
        PAIRING_STATE_PAIRING_REQUIRED,
        PAIRING_STATE_REVOKED,
    }:
        return False
    if BACKOFF_DELAYS_SECONDS != (1, 2, 4, 8, 16, 30):
        return False
    if MAX_BACKOFF_SECONDS != 30:
        return False
    return len(PAIRING_STEPS) == 6
