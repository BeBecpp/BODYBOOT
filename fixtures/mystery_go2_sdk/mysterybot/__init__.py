"""MysteryBot M2 quadruped SDK - Python binding.

This build ships ONLY the simulator transport (``sim://``). It performs no
network I/O and talks to no hardware; every channel is fully deterministic.

Typical use::

    from mysterybot import RobotSession

    link = RobotSession()
    link.open()
    frame = link.video().fetch_rgb()
    link.close()
"""

from .camera import Frame, VideoPipe
from .client import (
    DEFAULT_ENDPOINT,
    LinkReport,
    RobotSession,
    SessionClosedError,
    TransportUnavailableError,
)
from .sport import SportChannel, StickCommand
from .state import BatteryPacket, ImuPacket, StateChannel, StatePacket

SDK_VERSION = "0.9.3"

__all__ = [
    "DEFAULT_ENDPOINT",
    "SDK_VERSION",
    "BatteryPacket",
    "Frame",
    "ImuPacket",
    "LinkReport",
    "RobotSession",
    "SessionClosedError",
    "SportChannel",
    "StatePacket",
    "StateChannel",
    "StickCommand",
    "TransportUnavailableError",
    "VideoPipe",
]
