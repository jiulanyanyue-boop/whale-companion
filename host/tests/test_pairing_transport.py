"""Pairing uses the advertised TLS certificate and persistent QR credentials."""
import json
import sqlite3
import ssl
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from astrbot_whale_device.pairing import PairingTokenStore
from astrbot_whale_device.pairing_code import build_presentation
from astrbot_whale_device.pairing_endpoint import build_pairing_service, start_pairing_server
from astrbot_whale_device.tls_cert import ensure_self_signed_certificate


def test_pairing_https_and_qr_credentials_survive_separate_connections(tmp_path):
    cert = tmp_path / 'cert.pem'
    key = tmp_path / 'key.pem'
    ensure_self_signed_certificate(certfile=cert, keyfile=key, include_local_addresses=False)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    db_path = str(tmp_path / 'pairing.sqlite3')
    with sqlite3.connect(db_path) as connection:
        record = PairingTokenStore(connection).issue(1000, code_factory=lambda: 'ABCD2345')
    presentation = build_presentation(record=record, host_fingerprint='a' * 64,
        phone_urls=['wss://127.0.0.1:8765/device'])
    query = parse_qs(urlparse(presentation.qr_payload).query)
    assert query['code'] == [record.pairing_code]
    service = build_pairing_service(db_path)
    server, thread, port = start_pairing_server(host='127.0.0.1', port=0,
        service=service, ssl_context=tls)
    try:
        context = ssl.create_default_context(cafile=str(cert))
        with urllib.request.urlopen(f'https://localhost:{port}/pair/hostinfo', context=context, timeout=3) as response:
            assert json.load(response)['ok'] is True
        with pytest.raises(Exception):
            urllib.request.urlopen(f'https://localhost:{port}/pair/hostinfo', timeout=3)
        assert service.submit_code(query['token'][0], query['code'][0], now_ms=1001).used
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
