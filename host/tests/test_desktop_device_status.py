"""Desktop connection state and application-list protocol, without a phone."""
import base64
import json
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from astrbot_whale_device.host_gui import HostWindow, AppEntry
from astrbot_whale_device.models import DeviceResponse
from astrbot_whale_device.pairing import canonical_bytes
from astrbot_whale_device.standalone import StandaloneHost
from astrbot_whale_device.tls_cert import ensure_self_signed_certificate


@dataclass
class Config:
    device_id: str


class Widget:
    def __init__(self):
        self.values = {}
    def configure(self, **values):
        self.values.update(values)


def window(tmp_path):
    path = tmp_path / 'host.json'
    path.write_text(json.dumps({'device_id': 'device-pending', 'keep': 123}))
    with sqlite3.connect(tmp_path / 'pairing.sqlite3') as db:
        db.execute('CREATE TABLE paired_devices (device_id TEXT, state TEXT)')
        db.execute('INSERT INTO paired_devices VALUES (?, ?)', ('phone-real', 'PAIRED_OFFLINE'))
    states = []
    host = SimpleNamespace(config=Config('device-pending'), plugin=SimpleNamespace(config=Config('device-pending')),
        device_server=SimpleNamespace(connections=lambda: states))
    win = HostWindow.__new__(HostWindow)
    win.config_path = str(path)
    win.host_obj = host
    win._connected_device_id = ''
    win._fetching_apps = False
    win.fetch_apps_btn = Widget()
    win.app_count_label = Widget()
    win._log = lambda text: None
    return win, states


def test_online_paired_phone_enables_button_and_syncs_identity(tmp_path):
    win, states = window(tmp_path)
    win._update_device_connection()
    assert win.fetch_apps_btn.values['state'] == 'disabled'
    states.append(SimpleNamespace(device_id='phone-real', state='ONLINE'))
    win._update_device_connection()
    assert win.fetch_apps_btn.values['state'] == 'normal'
    assert win.host_obj.config.device_id == 'phone-real'
    assert win.host_obj.plugin.config.device_id == 'phone-real'
    saved = json.loads((tmp_path / 'host.json').read_text())
    assert saved['device_id'] == 'phone-real' and saved['keep'] == 123
    win._fetching_apps = True
    win._update_device_connection()
    assert win.fetch_apps_btn.values['state'] == 'disabled'
    win._fetching_apps = False
    states.clear()
    win._update_device_connection()
    assert win.fetch_apps_btn.values['state'] == 'disabled'
    assert win._connected_device_id == ''


@pytest.mark.parametrize('device,state', [('phone-real', 'CONNECTING'), ('unknown-phone', 'ONLINE')])
def test_unbound_or_unpaired_connection_does_not_enable_button(tmp_path, device, state):
    win, states = window(tmp_path)
    states.append(SimpleNamespace(device_id=device, state=state))
    win._update_device_connection()
    assert win.fetch_apps_btn.values['state'] == 'disabled'


def test_app_list_uses_selected_phone_and_surfaces_denial(tmp_path):
    win, states = window(tmp_path)
    win._connected_device_id = 'phone-real'
    calls = []
    def request(command, payload, *, device_id):
        calls.append((command, payload, device_id))
        return DeviceResponse('req', device_id, 'ok', evidence={'packages': 'a.b\nc.d\n'})
    win.host_obj.request_device = request
    assert win._request_app_list(win.host_obj) == [AppEntry('a.b', 'a.b'), AppEntry('c.d', 'c.d')]
    assert calls == [('device.app.list', {}, 'phone-real')]
    win.host_obj.request_device = lambda *a, **kw: DeviceResponse('req', 'phone-real', 'denied', 'SIGNATURE_REQUIRED', 'bad signature')
    with pytest.raises(RuntimeError, match='SIGNATURE_REQUIRED'):
        win._request_app_list(win.host_obj)


def test_desktop_envelope_has_real_certificate_signature_and_protocol_fields(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from astrbot_whale_device.protocol import decode_envelope
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    ensure_self_signed_certificate(certfile=cert, keyfile=key, include_local_addresses=False)
    host = StandaloneHost.__new__(StandaloneHost)
    host.config = SimpleNamespace(device_server=SimpleNamespace(keyfile=str(key)))
    frames = []
    def receive(envelope):
        frames.append(envelope)
        fields = envelope.as_dict()
        decode_envelope(json.dumps(fields))
        public = x509.load_pem_x509_certificate(cert.read_bytes()).public_key()
        public.verify(base64.b64decode(envelope.signature), canonical_bytes(fields), ec.ECDSA(hashes.SHA256()))
        return DeviceResponse(envelope.request_id, envelope.device_id, 'ok')
    host.transport = SimpleNamespace(request=receive)
    for _ in range(2):
        assert host.request_device('device.app.list', {}, device_id='phone-real').status == 'ok'
    assert frames[1].counter > frames[0].counter
    assert frames[0].idempotency_key != frames[1].idempotency_key


def test_application_list_over_real_wss_with_phone_simulator(tmp_path):
    import ssl
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from websockets.sync.client import connect
    from astrbot_whale_device.device_server import WhaleDeviceServer, build_heartbeat_frame
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    ensure_self_signed_certificate(certfile=cert, keyfile=key, include_local_addresses=False)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    server = WhaleDeviceServer(host='127.0.0.1', port=0, ssl_context=server_tls)
    server.start()
    ready = threading.Event()
    def phone():
        with connect(f'wss://localhost:{server.base_port}/device', ssl=client_tls) as connection:
            connection.send(json.dumps(build_heartbeat_frame('phone-real')))
            ready.set()
            fields = json.loads(connection.recv(timeout=5))
            assert fields['command'] == 'device.app.list'
            assert fields['device_id'] == 'phone-real'
            assert fields['signature']
            connection.send(json.dumps(DeviceResponse(fields['request_id'], 'phone-real', 'ok',
                evidence={'packages': 'com.example.one\ncom.example.two'}).as_dict()))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(phone)
            assert ready.wait(5)
            host = StandaloneHost.__new__(StandaloneHost)
            host.config = SimpleNamespace(device_server=SimpleNamespace(keyfile=str(key)))
            host.transport = server
            response = host.request_device('device.app.list', {}, device_id='phone-real')
            assert response.status == 'ok'
            assert response.evidence['packages'].splitlines() == ['com.example.one', 'com.example.two']
            task.result(timeout=5)
    finally:
        server.stop()
