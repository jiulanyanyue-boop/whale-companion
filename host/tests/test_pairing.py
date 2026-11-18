"""M05 主机侧配对、身份与签名校验。

覆盖：canonical JSON 与 Android 侧一致、指纹、六步配对、一次性 token、
指纹变化拒绝、退避、错误码纪律。
"""

import hashlib
import json
import sqlite3

import pytest

from astrbot_whale_device.pairing import (
    BACKOFF_DELAYS_SECONDS,
    FINGERPRINT_HEX_LENGTH,
    MAX_BACKOFF_SECONDS,
    PAIRING_STATE_PAIRING_REQUIRED,
    PAIRING_STATE_PAIRED_OFFLINE,
    PAIRING_STATE_REVOKED,
    PAIRING_STEPS,
    PAIRING_STATES,
    PAIRING_TOKEN_TTL_MS,
    PairingError,
    PairingService,
    PairingTokenStore,
    backoff_delay_ms,
    backoff_delay_seconds,
    canonical_bytes,
    canonical_text,
    fingerprint_matches,
    fingerprint_of,
    host_identity_from_public_key,
    is_well_formed_fingerprint,
    pairing_states_are_fixed,
    verify_envelope_signature,
)
from astrbot_whale_device.security import canonical_payload

HOST_FP = "a" * 64
OTHER_FP = "b" * 64


@pytest.fixture()
def service():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    svc = PairingService(connection, clock=lambda: 1_000_000)
    try:
        yield svc
    finally:
        connection.close()


def paired_service(service: PairingService, device_id: str = "device-1", public_key: str = "pk-1"):
    token = service.issue_token(1_000_000)
    service.set_host_identity(host_identity_from_public_key("host-1", b"host-public-key"))
    service.submit_code(token.token, token.pairing_code, 1_000_000)
    service.register_device_identity(device_id, public_key)
    service.save_device(device_id, capabilities_version="caps-v1", connection_config="cfg")
    service.confirm_fingerprint(device_id, user_accepted=True, observed_host_fingerprint=service.host_fingerprint())
    service.finalize(token.token)
    return token


# ---------------------------------------------------------------------------
# canonical JSON 与 Android 侧一致
# ---------------------------------------------------------------------------


def test_canonical_matches_security_canonical_payload():
    fields = {"command": "device.device_status", "counter": 1, "signature": "S"}
    unsigned = {"command": "device.device_status", "counter": 1}
    assert canonical_bytes(fields) == canonical_payload(unsigned)
    assert canonical_text(fields) == canonical_payload(unsigned).decode("utf-8")


def test_canonical_drops_only_top_level_signature():
    fields = {"signature": "S", "nested": {"signature": "inner"}}
    text = canonical_text(fields)
    assert text == '{"nested":{"signature":"inner"}}'


def test_canonical_is_sorted_and_compact():
    text = canonical_text({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'
    assert " " not in text


def test_canonical_keeps_non_ascii_raw():
    text = canonical_text({"text": "你好，鲸鱼娘🐋", "signature": "S"})
    assert "你好" in text
    assert "🐋" in text
    assert "\\u" not in text


def test_canonical_escape_rules():
    """与 Android `CanonicalJson` 的转义规则逐条一致。"""
    text = canonical_text({"ctrl": "\u0001\u0002", "backspace": "\b", "formfeed": "\f", "signature": "S"})
    assert "\\u0001\\u0002" in text
    assert "\\b" in text
    assert "\\f" in text


def test_canonical_does_not_escape_del_or_slash():
    text = canonical_text({"del_char": "\x7f", "slash": "a/b", "signature": "S"})
    assert "\\u007f" not in text
    assert "\x7f" in text
    assert "\\/" not in text


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------


def test_fingerprint_is_lowercase_sha256_hex():
    fingerprint = fingerprint_of(b"device-public-key")
    assert fingerprint == "9ec54f7d284f41086f89f126a22b32e55bd376649108b068c0ccb9402e44ea59"
    assert len(fingerprint) == FINGERPRINT_HEX_LENGTH
    assert fingerprint == hashlib.sha256(b"device-public-key").hexdigest()


def test_fingerprint_matches_android_side_values():
    """与 Kotlin `CanonicalJsonTest` 里硬编码的实测值一致。"""
    assert fingerprint_of(b"pk") == "eb3102a6cb586765d01fad324523ec0bc67b9efd6a2d9589c135adfedf7922cc"


def test_fingerprint_matches_is_exact():
    value = fingerprint_of(b"k")
    assert fingerprint_matches(value, value) is True
    assert fingerprint_matches(value, value.upper()) is False
    assert fingerprint_matches(value, value + "x") is False
    assert fingerprint_matches("", "") is True
    assert fingerprint_matches("", value) is False


def test_well_formed_fingerprint():
    assert is_well_formed_fingerprint(fingerprint_of(b"pk")) is True
    assert is_well_formed_fingerprint("XYZ") is False
    assert is_well_formed_fingerprint("") is False
    assert is_well_formed_fingerprint(None) is False


# ---------------------------------------------------------------------------
# 退避
# ---------------------------------------------------------------------------


def test_backoff_sequence_is_fixed():
    assert BACKOFF_DELAYS_SECONDS == (1, 2, 4, 8, 16, 30)
    assert MAX_BACKOFF_SECONDS == 30
    assert backoff_delay_seconds(1) == 1
    assert backoff_delay_seconds(3) == 4
    assert backoff_delay_seconds(6) == 30
    assert backoff_delay_seconds(7) == 30
    assert backoff_delay_seconds(100) == 30


def test_backoff_ms():
    assert backoff_delay_ms(1) == 1_000
    assert backoff_delay_ms(6) == 30_000


def test_state_machine_selfcheck():
    assert pairing_states_are_fixed() is True
    assert len(PAIRING_STEPS) == 6
    assert PAIRING_STATES == {
        "UNPAIRED", "PAIRING", "PAIRED_OFFLINE", "PAIRING_REQUIRED", "REVOKED",
    }


# ---------------------------------------------------------------------------
# 六步配对
# ---------------------------------------------------------------------------


def test_six_step_pairing_succeeds(service):
    token = paired_service(service)
    record = service.devices.get("device-1")
    assert record is not None
    assert record.state == PAIRING_STATE_PAIRED_OFFLINE
    assert record.capabilities_version == "caps-v1"
    assert record.connection_config == "cfg"
    assert is_well_formed_fingerprint(record.fingerprint)
    # 第 6 步：token 立即失效。
    assert service.tokens.is_usable(token.token, 1_000_000) is False


def test_token_is_single_use(service):
    token = service.issue_token(1_000_000)
    service.submit_code(token.token, token.pairing_code, 1_000_000)
    with pytest.raises(PairingError) as exc:
        service.submit_code(token.token, token.pairing_code, 1_000_000)
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_expired_token_is_rejected(service):
    token = service.issue_token(1_000_000, ttl_ms=1_000)
    with pytest.raises(PairingError) as exc:
        service.submit_code(token.token, token.pairing_code, 1_002_000)
    assert exc.value.code == "EXPIRED"


def test_wrong_code_is_rejected(service):
    token = service.issue_token(1_000_000)
    with pytest.raises(PairingError) as exc:
        service.submit_code(token.token, "999999", 1_000_000)
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_unknown_token_is_rejected(service):
    with pytest.raises(PairingError) as exc:
        service.submit_code("nope", "000000", 1_000_000)
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_token_ttl_default():
    assert PAIRING_TOKEN_TTL_MS == 10 * 60 * 1000


def test_finalize_makes_token_unusable(service):
    token = service.issue_token(1_000_000)
    service.submit_code(token.token, token.pairing_code, 1_000_000)
    service.finalize(token.token)
    assert service.tokens.is_usable(token.token, 1_000_000) is False
    with pytest.raises(PairingError):
        service.tokens.consume(token.token, 1_000_000)


def test_concurrent_token_consumption_only_one_wins(tmp_path):
    """条件 UPDATE 保证并发下只有一次成功（不是内存 Map）。"""
    import threading

    connection = sqlite3.connect(str(tmp_path / "pairing.db"), check_same_thread=False)
    try:
        tokens = PairingTokenStore(connection)
        record = tokens.issue(1_000_000, code_factory=lambda: "111111", token_factory=lambda: "tok")
        winners = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                tokens.consume("tok", 1_000_000)
                with lock:
                    winners.append(True)
            except PairingError:
                with lock:
                    errors.append(True)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(winners) == 1
        assert len(errors) == 5
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 身份与指纹变化
# ---------------------------------------------------------------------------


def test_declared_fingerprint_must_match_public_key(service):
    with pytest.raises(PairingError) as exc:
        service.register_device_identity("device-1", "pk-1", fingerprint=OTHER_FP)
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_device_public_key_change_requires_repairing(service):
    service.register_device_identity("device-1", "pk-1")
    with pytest.raises(PairingError) as exc:
        service.register_device_identity("device-1", "pk-2")
    assert exc.value.code == "SIGNATURE_REQUIRED"
    assert service.devices.get("device-1").state == PAIRING_STATE_PAIRING_REQUIRED


def test_host_fingerprint_change_is_refused(service):
    service.set_host_identity(host_identity_from_public_key("host-1", b"host-public-key"))
    with pytest.raises(PairingError) as exc:
        service.assert_host_fingerprint(OTHER_FP)
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_host_identity_rejects_malformed_fingerprint(service):
    from astrbot_whale_device.pairing import HostIdentity

    with pytest.raises(PairingError):
        service.set_host_identity(HostIdentity("host-1", "pk", "short"))


def test_confirm_rejected_by_user(service):
    token = service.issue_token(1_000_000)
    service.set_host_identity(host_identity_from_public_key("host-1", b"host-public-key"))
    service.submit_code(token.token, token.pairing_code, 1_000_000)
    service.register_device_identity("device-1", "pk-1")
    service.save_device("device-1", capabilities_version="caps")
    with pytest.raises(PairingError) as exc:
        service.confirm_fingerprint(
            "device-1", user_accepted=False, observed_host_fingerprint=service.host_fingerprint()
        )
    assert exc.value.code == "ACTION_REJECTED"
    assert service.devices.get("device-1").state == PAIRING_STATE_PAIRING_REQUIRED


def test_confirm_checks_host_fingerprint(service):
    token = service.issue_token(1_000_000)
    service.set_host_identity(host_identity_from_public_key("host-1", b"host-public-key"))
    service.submit_code(token.token, token.pairing_code, 1_000_000)
    service.register_device_identity("device-1", "pk-1")
    service.save_device("device-1")
    with pytest.raises(PairingError) as exc:
        service.confirm_fingerprint("device-1", user_accepted=True, observed_host_fingerprint=OTHER_FP)
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_revoke_device(service):
    service.register_device_identity("device-1", "pk-1")
    service.revoke_device("device-1")
    assert service.devices.get("device-1").state == PAIRING_STATE_REVOKED


def test_heartbeat_after_revoke_is_refused(service):
    service.register_device_identity("device-1", "pk-1")
    service.revoke_device("device-1")
    with pytest.raises(PairingError):
        service.on_heartbeat("device-1")


def test_host_identity_has_no_private_key_field():
    identity = host_identity_from_public_key("host-1", b"host-public-key")
    fields = set(identity.__dataclass_fields__)
    assert "private" not in " ".join(fields).lower()
    assert fields == {"host_id", "public_key_encoded", "fingerprint"}


def test_device_record_has_no_private_key_field(service):
    service.register_device_identity("device-1", "pk-1")
    record = service.devices.get("device-1")
    fields = set(record.__dataclass_fields__)
    assert not any("private" in name or "secret" in name for name in fields)


# ---------------------------------------------------------------------------
# 签名校验
# ---------------------------------------------------------------------------


def test_signature_must_be_present():
    with pytest.raises(PairingError) as exc:
        verify_envelope_signature(
            {},
            device_public_key_encoded="pk",
            expected_device_fingerprint=fingerprint_of(b"pk"),
            expected_host_fingerprint=HOST_FP,
            observed_host_fingerprint=HOST_FP,
            verifier=lambda key, payload, signature: True,
        )
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_device_fingerprint_is_checked_not_just_device_id():
    """禁止项：只校验 device_id 不校验公钥指纹。"""
    with pytest.raises(PairingError) as exc:
        verify_envelope_signature(
            {"signature": "sig"},
            device_public_key_encoded="pk",
            expected_device_fingerprint=OTHER_FP,
            expected_host_fingerprint=HOST_FP,
            observed_host_fingerprint=HOST_FP,
            verifier=lambda key, payload, signature: True,
        )
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_host_fingerprint_change_during_verification_is_refused():
    with pytest.raises(PairingError) as exc:
        verify_envelope_signature(
            {"signature": "sig"},
            device_public_key_encoded="pk",
            expected_device_fingerprint=fingerprint_of(b"pk"),
            expected_host_fingerprint=HOST_FP,
            observed_host_fingerprint=OTHER_FP,
            verifier=lambda key, payload, signature: True,
        )
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_crypto_failure_is_refused():
    with pytest.raises(PairingError) as exc:
        verify_envelope_signature(
            {"signature": "sig"},
            device_public_key_encoded="pk",
            expected_device_fingerprint=fingerprint_of(b"pk"),
            expected_host_fingerprint=HOST_FP,
            observed_host_fingerprint=HOST_FP,
            verifier=lambda key, payload, signature: False,
        )
    assert exc.value.code == "SIGNATURE_REQUIRED"


def test_valid_envelope_passes():
    captured = {}

    def verifier(key, payload, signature):
        captured["payload"] = payload
        captured["signature"] = signature
        return True

    fields = {"command": "device.device_status", "counter": 1, "signature": "sig"}
    verify_envelope_signature(
        fields,
        device_public_key_encoded="pk",
        expected_device_fingerprint=fingerprint_of(b"pk"),
        expected_host_fingerprint=HOST_FP,
        observed_host_fingerprint=HOST_FP,
        verifier=verifier,
    )
    # 验签输入必须是 canonical 字节（去掉 signature 后的那份）。
    assert captured["payload"] == canonical_bytes(fields)
    assert b"signature" not in captured["payload"]


# ---------------------------------------------------------------------------
# 错误码纪律
# ---------------------------------------------------------------------------


def test_pairing_never_invents_new_error_codes():
    """pairing.py 里出现的错误码字符串必须全部在 Kotlin CommandErrors.ALL 里登记。

    `PAIRING_REQUIRED` 只是状态名，不允许作为 error_code 传给主机。
    """
    import inspect
    import pathlib
    import re

    import astrbot_whale_device.pairing as pairing_module

    kotlin = (pathlib.Path(__file__).resolve().parents[2] / "device/app/src/main/java/cn/whale/agent/device/CommandErrors.kt").read_text(encoding="utf-8")
    block = re.search(r"val ALL: Set<String> = setOf\((.+?)\n    \)", kotlin, re.S)
    assert block, "could not locate CommandErrors.ALL"
    registered = set(re.findall(r"[A-Z][A-Z0-9_]{3,}", block.group(1))) - {"ALL"}

    source = inspect.getsource(pairing_module)
    used = set(re.findall(r'PairingError\s*\(\s*"([A-Z][A-Z0-9_]+)"', source))
    assert used, "expected at least one PairingError code in pairing.py"
    assert used <= registered, f"unregistered error codes: {sorted(used - registered)}"
    # 状态名永远不能当错误码。
    assert "PAIRING_REQUIRED" not in used
