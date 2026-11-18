"""A QQ device task uses authenticated host certificate signatures, only after pairing."""
import base64
import sqlite3
from types import SimpleNamespace
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from astrbot_whale_device.desktop_device_transport import DesktopDeviceTransport
from astrbot_whale_device.models import DeviceEnvelope,DeviceResponse
from astrbot_whale_device.pairing import canonical_bytes
from astrbot_whale_device.tls_cert import ensure_self_signed_certificate


def test_device_adapter_enforces_pairing_and_resigns_with_monotonic_counter(tmp_path):
    cert,key=tmp_path/'cert.pem',tmp_path/'key.pem'
    ensure_self_signed_certificate(certfile=cert,keyfile=key,include_local_addresses=False)
    received=[]
    host=SimpleNamespace(config=SimpleNamespace(device_server=SimpleNamespace(keyfile=str(key))),
        transport=SimpleNamespace(request=lambda e:received.append(e) or DeviceResponse(e.request_id,e.device_id,'ok')))
    adapter=DesktopDeviceTransport(host,tmp_path/'host.json')
    envelope=DeviceEnvelope(1,'request','phone-test','device.app.list',8000,'idempotent',{},counter=1,signature='old-hmac')
    assert adapter.request(envelope).status=='denied'
    with sqlite3.connect(tmp_path/'pairing.sqlite3') as db:
        db.execute('CREATE TABLE paired_devices(device_id TEXT,state TEXT)')
        db.execute('INSERT INTO paired_devices VALUES (?,?)',('phone-test','PAIRING'))
    assert adapter.request(envelope).status=='denied'
    with sqlite3.connect(tmp_path/'pairing.sqlite3') as db:db.execute("UPDATE paired_devices SET state='PAIRED_OFFLINE'")
    for _ in range(2):assert adapter.request(envelope).status=='ok'
    public=x509.load_pem_x509_certificate(cert.read_bytes()).public_key()
    assert received[1].counter>received[0].counter
    for e in received:
        assert e.idempotency_key=='idempotent'
        public.verify(base64.b64decode(e.signature),canonical_bytes(e.as_dict()),ec.ECDSA(hashes.SHA256()))
