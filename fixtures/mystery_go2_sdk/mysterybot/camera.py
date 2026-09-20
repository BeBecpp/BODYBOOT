"""Camera channel of the MysteryBot SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import RobotSession

FRAME_COLS = 160
"""Width of the head camera image in pixels (simulator resolution)."""

FRAME_ROWS = 90
"""Height of the head camera image in pixels (simulator resolution)."""

FRAME_PERIOD_US = 33_333
"""Nominal time between two frames, microseconds (30 fps)."""


@dataclass(frozen=True)
class Frame:
    """One image from the head camera.

    Attributes:
        cols: Image width in pixels.
        rows: Image height in pixels.
        fmt: Pixel layout tag. ``"RGB24"`` is packed 8-bit R,G,B interleaved,
            row-major, 3 bytes per pixel. ``"Y8"`` is 8-bit luminance, 1 byte per pixel.
        buf: Raw pixel bytes, ``cols * rows * bytes_per_pixel`` long.
        t_us: Capture time in microseconds since the session was opened.
        seq: Frame counter, starts at 1.
    """

    cols: int
    rows: int
    fmt: str
    buf: bytes
    t_us: int
    seq: int


class VideoPipe:
    """Pull-style access to the robot's forward-facing head camera."""

    def __init__(self, session: RobotSession) -> None:
        self._session = session
        self._seq = 0
        self._exposure_ev = 0.0

    def _next(self) -> int:
        self._session._require_open()
        self._seq += 1
        return self._seq

    def fetch_rgb(self) -> Frame:
        """Grab the newest colour image from the head camera.

        The frame is expressed in the camera optical frame (z forward, x right,
        y down) and delivered as ``RGB24``. Read-only.
        """
        n = self._next()
        row = bytes(
            channel
            for col in range(FRAME_COLS)
            for channel in ((col + n) % 256, (2 * col + 3 * n) % 256, (255 - col + n) % 256)
        )
        return Frame(
            cols=FRAME_COLS,
            rows=FRAME_ROWS,
            fmt="RGB24",
            buf=row * FRAME_ROWS,
            t_us=n * FRAME_PERIOD_US,
            seq=n,
        )

    def fetch_mono(self) -> Frame:
        """Grab the newest image as 8-bit luminance (``Y8``), e.g. for fiducial trackers."""
        n = self._next()
        row = bytes((col + n) % 256 for col in range(FRAME_COLS))
        return Frame(
            cols=FRAME_COLS,
            rows=FRAME_ROWS,
            fmt="Y8",
            buf=row * FRAME_ROWS,
            t_us=n * FRAME_PERIOD_US,
            seq=n,
        )

    def set_exposure(self, ev: float) -> None:
        """Change the exposure compensation of the head camera.

        Args:
            ev: Exposure compensation in EV stops, -2.0 .. +2.0.
        """
        self._session._require_open()
        self._exposure_ev = max(-2.0, min(2.0, ev))
