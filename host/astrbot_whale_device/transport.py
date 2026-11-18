from __future__ import annotations

from collections.abc import Callable

from .models import DeviceEnvelope, DeviceResponse
from .protocol import ALLOWED_COMMANDS


class TransportError(RuntimeError):
    pass


class DeviceTransport:
    def request(self, envelope: DeviceEnvelope) -> DeviceResponse:
        raise NotImplementedError


class InMemoryDeviceTransport(DeviceTransport):
    def __init__(self, handler: Callable[[DeviceEnvelope], DeviceResponse]) -> None:
        self.handler = handler

    def request(self, envelope: DeviceEnvelope) -> DeviceResponse:
        if envelope.command not in ALLOWED_COMMANDS:
            raise TransportError("unknown command")
        return self.handler(envelope)

