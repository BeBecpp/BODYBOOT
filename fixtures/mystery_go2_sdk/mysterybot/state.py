"""Telemetry channel of the MysteryBot SDK.

Telemetry follows the ISO 8855 convention: x forward, y left, z up and
counter-clockwise-positive angles. (The locomotion *command* API in
``sport`` historically does not - read its documentation carefully.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import RobotSession

STATE_PERIOD_US = 20_000
"""Time between two fused state packets, microseconds (50 Hz)."""

IMU_PERIOD_US = 5_000
"""Time between two inertial packets, microseconds (200 Hz)."""

STANDARD_GRAVITY = 9.80665
"""One g expressed in m/s^2."""


def _wrap_deg(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def _heading_at(n: int) -> float:
    return round(_wrap_deg(179.4 + 0.3 * n), 4)


@dataclass(frozen=True)
class StatePacket:
    """Fused body state from leg odometry, 50 Hz.

    Attributes:
        seq: Packet counter, starts at 1.
        t_us: Timestamp in microseconds since the session was opened.
        pos: Body position (x, y, z) in metres, expressed in the ``odom`` frame whose
            origin is where the session was opened (x forward, y left, z up).
        heading_deg: Body yaw in the ``odom`` frame, in DEGREES, range [-180, 180),
            counter-clockwise positive seen from above.
        body_vel: Linear velocity (forward, left) in m/s, expressed in the body frame.
        turn_rate_dps: Yaw rate in degrees per second, counter-clockwise positive.
        gait: Active gait id (0 idle, 1 trot, 2 run).
        foot_force: Contact force of the four feet (FL, FR, RL, RR) in newtons.
    """

    seq: int
    t_us: int
    pos: tuple[float, float, float]
    heading_deg: float
    body_vel: tuple[float, float]
    turn_rate_dps: float
    gait: int
    foot_force: tuple[float, float, float, float]


@dataclass(frozen=True)
class ImuPacket:
    """Raw inertial sample from the body IMU, 200 Hz.

    Attributes:
        t_us: Timestamp in microseconds since the session was opened.
        accel_g: Specific force (x, y, z) in the body frame, in units of standard
            gravity g (1 g = 9.80665 m/s^2). Reads about (0, 0, +1) when standing still.
        gyro_dps: Angular rate (x, y, z) in the body frame, in degrees per second.
        quat_wxyz: Body orientation as a unit quaternion, scalar FIRST: (w, x, y, z).
        temp_c: IMU die temperature in degrees Celsius.
    """

    t_us: int
    accel_g: tuple[float, float, float]
    gyro_dps: tuple[float, float, float]
    quat_wxyz: tuple[float, float, float, float]
    temp_c: float


@dataclass(frozen=True)
class BatteryPacket:
    """Battery management system report.

    Attributes:
        soc_pct: State of charge, 0-100 percent.
        voltage_mv: Pack voltage in millivolts.
        current_ma: Pack current in milliamps, negative while discharging.
        cycles: Completed charge cycles.
    """

    soc_pct: int
    voltage_mv: int
    current_ma: int
    cycles: int


class StateChannel:
    """Pull-style access to robot telemetry. Every call here is read-only."""

    def __init__(self, session: RobotSession) -> None:
        self._session = session
        self._state_seq = 0
        self._imu_seq = 0
        self._battery_reads = 0

    def read_packet(self) -> StatePacket:
        """Read the newest fused body state (where the robot is and how it moves)."""
        self._session._require_open()
        self._state_seq += 1
        n = self._state_seq
        return StatePacket(
            seq=n,
            t_us=n * STATE_PERIOD_US,
            pos=(round(0.004 * n, 4), round(-0.001 * n, 4), 0.31),
            heading_deg=_heading_at(n),
            body_vel=(round(0.20 + 0.01 * (n % 5), 4), -0.05),
            turn_rate_dps=15.0,
            gait=1,
            foot_force=(41.0, 39.5, 40.2, 40.8),
        )

    def read_imu_packet(self) -> ImuPacket:
        """Read the newest raw inertial sample."""
        self._session._require_open()
        self._imu_seq += 1
        n = self._imu_seq
        half_yaw = math.radians(_heading_at(n)) / 2.0
        return ImuPacket(
            t_us=n * IMU_PERIOD_US,
            accel_g=(round(0.01 * ((n % 3) - 1), 4), -0.02, 0.9996),
            gyro_dps=(1.5, -0.5, round(15.0 + 0.1 * n, 4)),
            quat_wxyz=(round(math.cos(half_yaw), 6), 0.0, 0.0, round(math.sin(half_yaw), 6)),
            temp_c=41.5,
        )

    def read_battery(self) -> BatteryPacket:
        """Read the battery management system report."""
        self._session._require_open()
        self._battery_reads += 1
        return BatteryPacket(soc_pct=87, voltage_mv=29_400, current_ma=-3_150, cycles=42)
