"""Locomotion command channel of the MysteryBot SDK.

WARNING: everything in this module except last_command() commands the robot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import RobotSession

MAX_ADVANCE_MPS = 1.2
"""Largest accepted advance (forward/backward) speed, m/s."""

MAX_STRAFE_MPS = 0.4
"""Largest accepted strafe (sideways) speed, m/s."""

MAX_TURN_DPS = 100.0
"""Largest accepted turn speed, degrees per second."""

SETPOINT_TTL_S = 0.4
"""A velocity setpoint expires after this many seconds without refresh."""


@dataclass(frozen=True)
class StickCommand:
    """Echo of an accepted virtual-stick setpoint (after clipping).

    Attributes:
        v1: Strafe speed actually applied, m/s.
        v2: Advance speed actually applied, m/s.
        v3: Turn speed actually applied, degrees per second.
    """

    v1: float
    v2: float
    v3: float


def _clip(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class SportChannel:
    """High-level locomotion commands (the robot's 'sport mode')."""

    def __init__(self, session: RobotSession) -> None:
        self._session = session
        self._last: StickCommand | None = None
        self._log: list[str] = []

    def joystick(self, v1: float, v2: float, v3: float) -> StickCommand:
        """Send a virtual-stick velocity setpoint (same axes as the handheld remote).

        The robot keeps walking at the last setpoint until a new one arrives,
        freeze() is called, or the setpoint expires after SETPOINT_TTL_S seconds.

        NOTE: this uses the legacy remote-control axis convention, which differs
        from the telemetry convention used by StateChannel.

        Args:
            v1: Strafe speed in m/s. Positive moves the robot to ITS LEFT.
                Clipped to +/-MAX_STRAFE_MPS.
            v2: Advance speed in m/s. Positive walks FORWARD.
                Clipped to +/-MAX_ADVANCE_MPS.
            v3: Turn speed in DEGREES per second. Positive turns CLOCKWISE seen from
                above (to the robot's right), like pushing the stick to the right.
                Clipped to +/-MAX_TURN_DPS.

        Returns:
            The setpoint that was applied after clipping.
        """
        self._session._require_open()
        applied = StickCommand(
            v1=_clip(v1, MAX_STRAFE_MPS),
            v2=_clip(v2, MAX_ADVANCE_MPS),
            v3=_clip(v3, MAX_TURN_DPS),
        )
        self._last = applied
        self._log.append(f"joystick({applied.v1}, {applied.v2}, {applied.v3})")
        return applied

    def freeze(self) -> None:
        """Halt locomotion immediately.

        Zeroes the velocity setpoint; the robot stays standing with joints powered
        and balance control active. This is the normal way to stop walking and is
        always safe to call, including repeatedly.
        """
        self._session._require_open()
        self._last = StickCommand(0.0, 0.0, 0.0)
        self._log.append("freeze()")

    def go_limp(self) -> None:
        """Cut torque to every joint (passive damping mode).

        THE ROBOT WILL COLLAPSE TO THE GROUND. Last-resort emergency action only -
        this is NOT the normal way to stop walking; use freeze() for that.
        """
        self._session._require_open()
        self._last = None
        self._log.append("go_limp()")

    def posture(self, mode: int) -> None:
        """Change the body posture.

        Args:
            mode: 0 = lie down, 1 = stand up, 2 = sit.
        """
        self._session._require_open()
        self._log.append(f"posture({mode})")

    def last_command(self) -> StickCommand | None:
        """Return the currently active velocity setpoint, if any. Read-only."""
        self._session._require_open()
        return self._last
