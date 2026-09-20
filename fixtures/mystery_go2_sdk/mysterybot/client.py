"""Session / transport layer of the MysteryBot SDK."""

from __future__ import annotations

from dataclasses import dataclass

from .camera import VideoPipe
from .sport import SportChannel
from .state import StateChannel

DEFAULT_ENDPOINT = "sim://loopback"
"""Endpoint used when none is given. The ``sim://`` scheme selects the built-in simulator."""

KEEPALIVE_PERIOD_S = 0.5
"""Recommended interval between heartbeat() calls, seconds."""


class SessionClosedError(RuntimeError):
    """Raised when a channel is used before open() or after close()."""


class TransportUnavailableError(RuntimeError):
    """Raised for endpoint schemes this SDK build cannot serve."""


@dataclass(frozen=True)
class LinkReport:
    """Result of one keep-alive round trip.

    Attributes:
        link_up: True when the robot answered the keep-alive within the timeout.
        rtt_ms: Measured round-trip time of the keep-alive, in milliseconds.
        dropped: Number of keep-alives lost since the session was opened.
        uptime_s: Robot controller uptime in seconds.
    """

    link_up: bool
    rtt_ms: float
    dropped: int
    uptime_s: float


class RobotSession:
    """Owns the transport to one robot and hands out the data channels.

    A session must be opened with open() before any channel is used and should be
    closed with close() when done. Channels obtained from a session stay bound to it.
    """

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout_s: float = 2.0) -> None:
        """Create a session (no I/O happens here).

        Args:
            endpoint: Transport URI. Only ``sim://`` endpoints exist in this build.
            timeout_s: Per-request timeout in seconds.
        """
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._open = False
        self._beats = 0
        self._video: VideoPipe | None = None
        self._state: StateChannel | None = None
        self._sport: SportChannel | None = None

    @property
    def is_open(self) -> bool:
        """True between open() and close()."""
        return self._open

    def open(self) -> None:
        """Bring the transport up. Does not command the robot in any way."""
        if not self._endpoint.startswith("sim://"):
            raise TransportUnavailableError(
                f"endpoint {self._endpoint!r}: this SDK build only ships the simulator transport"
            )
        self._open = True

    def close(self) -> None:
        """Tear the transport down. Safe to call more than once."""
        self._open = False

    def _require_open(self) -> None:
        if not self._open:
            raise SessionClosedError("session is not open - call open() first")

    def heartbeat(self) -> LinkReport:
        """Round-trip one keep-alive and report link quality.

        Read-only: it never affects robot motion. Call it every
        KEEPALIVE_PERIOD_S seconds to supervise the connection.
        """
        self._require_open()
        self._beats += 1
        n = self._beats
        return LinkReport(
            link_up=True,
            rtt_ms=round(3.5 + 0.25 * (n % 4), 3),
            dropped=0,
            uptime_s=round(120.0 + KEEPALIVE_PERIOD_S * n, 3),
        )

    def firmware_info(self) -> dict[str, str]:
        """Static firmware / model identification strings."""
        self._require_open()
        return {"model": "M2", "firmware": "2.4.1-sim", "sdk": "0.9.3"}

    def video(self) -> VideoPipe:
        """Return the camera channel bound to this session (creates it on first use)."""
        if self._video is None:
            self._video = VideoPipe(self)
        return self._video

    def state(self) -> StateChannel:
        """Return the telemetry channel bound to this session (creates it on first use)."""
        if self._state is None:
            self._state = StateChannel(self)
        return self._state

    def sport(self) -> SportChannel:
        """Return the locomotion command channel bound to this session.

        Obtaining the channel sends nothing to the robot.
        """
        if self._sport is None:
            self._sport = SportChannel(self)
        return self._sport
