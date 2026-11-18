"""Adapt existing device plans to the certificate-signed desktop WSS transport."""
from dataclasses import replace
from pathlib import Path
import base64
import sqlite3
import threading
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from .models import DeviceResponse
from .pairing import canonical_bytes
from .standalone import next_desktop_counter


class DesktopDeviceTransport:
    def __init__(self, host, config_path):
        self.host = host
        self.db_path = Path(config_path).resolve().parent / 'pairing.sqlite3'
        self.lock = threading.Lock()
        self.key = serialization.load_pem_private_key(Path(host.config.device_server.keyfile).read_bytes(), password=None)

    def request(self, envelope):
        if not self.db_path.is_file():
            return DeviceResponse(envelope.request_id,envelope.device_id,'denied','SIGNATURE_REQUIRED','device is not paired')
        with sqlite3.connect(self.db_path) as db:
            row=db.execute('SELECT state FROM paired_devices WHERE device_id=?',(envelope.device_id,)).fetchone()
        if not row or row[0]!='PAIRED_OFFLINE':
            return DeviceResponse(envelope.request_id,envelope.device_id,'denied','SIGNATURE_REQUIRED','device is not paired')
        with self.lock:
            signed=replace(envelope,counter=next_desktop_counter(),issued_at_ms=time.time_ns()//1_000_000,signature='')
            signature=self.key.sign(canonical_bytes(signed.as_dict()),ec.ECDSA(hashes.SHA256()))
        return self.host.transport.request(replace(signed,signature=base64.b64encode(signature).decode('ascii')))
