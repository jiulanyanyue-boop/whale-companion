"""Whale Agent v2 host-side core.

The package deliberately keeps AstrBot imports at the integration boundary so
security and protocol behavior can be tested without running AstrBot.
"""

from .models import DeviceEnvelope, DeviceResponse, IncomingMessage, Principal
from .router import MessageRouter

__all__ = ["DeviceEnvelope", "DeviceResponse", "IncomingMessage", "MessageRouter", "Principal"]

