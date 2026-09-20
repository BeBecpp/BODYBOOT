"""The canonical embodiment contract - the *target* side of every mapping.

This module describes what BODYBOOT wants a robot to look like (names, units,
frames, sign conventions). It deliberately knows nothing about any vendor SDK
and contains no expected answers; it is safe to show to the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldShape = Literal["int", "float", "bool", "str", "bytes", "vec2", "vec3", "vec4"]
Risk = Literal["read_only", "motion", "stop"]
Kind = Literal["sensor", "actuator", "status"]

VECTOR_SIZES: dict[str, int] = {"vec2": 2, "vec3": 3, "vec4": 4}
NUMERIC_SHAPES: frozenset[str] = frozenset({"int", "float", "vec2", "vec3", "vec4"})


@dataclass(frozen=True)
class CanonicalField:
    """One field of a canonical sensor/status sample."""

    name: str
    shape: FieldShape
    unit: str | None
    description: str
    allowed_constants: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalParam:
    """One argument of a canonical actuator command."""

    name: str
    unit: str
    description: str


@dataclass(frozen=True)
class CanonicalCapability:
    """A capability every BODYBOOT embodiment must provide."""

    name: str
    kind: Kind
    risk: Risk
    adapter_method: str
    output_type: str | None
    description: str
    frame_hint: str | None = None
    fields: tuple[CanonicalField, ...] = ()
    params: tuple[CanonicalParam, ...] = ()

    def field(self, name: str) -> CanonicalField:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.name} has no canonical field {name!r}")


_STAMP = CanonicalField("stamp_s", "float", "s", "Sample time in seconds (any epoch, monotonic within a session).")

CAPABILITIES: tuple[CanonicalCapability, ...] = (
    CanonicalCapability(
        name="camera.rgb",
        kind="sensor",
        risk="read_only",
        adapter_method="camera_rgb",
        output_type="CameraFrame",
        description="Newest colour image from the primary forward camera.",
        frame_hint="camera_optical",
        fields=(
            CanonicalField("width", "int", "px", "Image width in pixels."),
            CanonicalField("height", "int", "px", "Image height in pixels."),
            CanonicalField(
                "encoding",
                "str",
                None,
                "Pixel encoding. Canonical value is 'rgb8': packed 8-bit R,G,B, row-major.",
                allowed_constants=("rgb8",),
            ),
            CanonicalField("data", "bytes", None, "Raw pixel bytes, width*height*3 long."),
            _STAMP,
        ),
    ),
    CanonicalCapability(
        name="imu",
        kind="sensor",
        risk="read_only",
        adapter_method="imu",
        output_type="ImuSample",
        description="Newest inertial sample of the robot body.",
        frame_hint="base_link",
        fields=(
            CanonicalField("accel_mps2", "vec3", "m/s^2", "Specific force (x, y, z); ~ (0, 0, +9.81) at rest."),
            CanonicalField("gyro_rps", "vec3", "rad/s", "Angular rate (x, y, z)."),
            CanonicalField(
                "orientation_xyzw",
                "vec4",
                None,
                "Unit quaternion, scalar LAST: (x, y, z, w).",
            ),
            _STAMP,
        ),
    ),
    CanonicalCapability(
        name="odometry",
        kind="sensor",
        risk="read_only",
        adapter_method="odometry",
        output_type="OdometrySample",
        description="Where the robot body is and how it is moving.",
        frame_hint="odom",
        fields=(
            CanonicalField("position_m", "vec3", "m", "Body position (x fwd, y left, z up) in the odom frame."),
            CanonicalField("yaw_rad", "float", "rad", "Body yaw, counter-clockwise positive seen from above."),
            CanonicalField("linear_velocity_mps", "vec2", "m/s", "Body-frame velocity (vx forward, vy left)."),
            CanonicalField("yaw_rate_rps", "float", "rad/s", "Yaw rate, counter-clockwise positive."),
            _STAMP,
        ),
    ),
    CanonicalCapability(
        name="locomotion.velocity",
        kind="actuator",
        risk="motion",
        adapter_method="velocity",
        output_type="MockMotionCommand",
        description="Body-frame velocity setpoint (REP-103).",
        frame_hint="base_link",
        params=(
            CanonicalParam("vx", "m/s", "Forward speed, positive = forward."),
            CanonicalParam("vy", "m/s", "Lateral speed, positive = to the robot's LEFT."),
            CanonicalParam("yaw_rate", "rad/s", "Turn rate, positive = COUNTER-clockwise seen from above."),
        ),
    ),
    CanonicalCapability(
        name="locomotion.stop",
        kind="actuator",
        risk="stop",
        adapter_method="stop",
        output_type=None,
        description=(
            "Halt locomotion and keep the robot standing and balanced. Must NOT be a command "
            "that removes joint torque or otherwise lets the robot fall."
        ),
        frame_hint=None,
    ),
    CanonicalCapability(
        name="connection.health",
        kind="status",
        risk="read_only",
        adapter_method="health",
        output_type="HealthSample",
        description="Is the link to the robot alive, and how slow is it.",
        frame_hint=None,
        fields=(
            CanonicalField("alive", "bool", None, "True when the robot is answering."),
            CanonicalField("latency_s", "float", "s", "Round-trip latency in seconds."),
        ),
    ),
)

REQUIRED_CAPABILITIES: tuple[str, ...] = tuple(cap.name for cap in CAPABILITIES)
MOTION_PARAMS: tuple[str, ...] = ("vx", "vy", "yaw_rate")

_BY_NAME: dict[str, CanonicalCapability] = {cap.name: cap for cap in CAPABILITIES}


def get_capability(name: str) -> CanonicalCapability:
    """Look a canonical capability up by name (KeyError if unknown)."""
    return _BY_NAME[name]


def is_canonical(name: str) -> bool:
    return name in _BY_NAME


def describe_contract() -> str:
    """Human/agent-readable rendering of the canonical contract."""
    lines: list[str] = []
    for cap in CAPABILITIES:
        lines.append(f"- {cap.name}  (kind={cap.kind}, risk={cap.risk})")
        lines.append(f"    {cap.description}")
        if cap.frame_hint:
            lines.append(f"    canonical frame: {cap.frame_hint}")
        for fld in cap.fields:
            unit = f" [{fld.unit}]" if fld.unit else ""
            const = f" (constant, one of {list(fld.allowed_constants)})" if fld.allowed_constants else ""
            lines.append(f"    field {fld.name}: {fld.shape}{unit} - {fld.description}{const}")
        for param in cap.params:
            lines.append(f"    param {param.name} [{param.unit}] - {param.description}")
    return "\n".join(lines)
