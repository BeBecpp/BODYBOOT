"""BODYBOOT safety policy - the immutable rules of the MVP.

1. Real motion is DISABLED. There is no switch, flag, environment variable or
   plan field that turns it on. ``REAL_MOTION_ENABLED`` is a constant.
2. Runtime probing only ever calls things that are provably read-only, and
   only on a transport that is provably simulated.
3. A plan that offers motion without a trustworthy stop gets no motion surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from bodyboot.discovery.models import CallableInfo, ClassInfo
from bodyboot.plan import EmbodimentPlan, PlanIssue

REAL_MOTION_ENABLED: Final[bool] = False
"""Milestone-1 invariant. Generated code copies this value; the verifier asserts it."""

MIN_ACTUATOR_CONFIDENCE: Final[float] = 0.5
"""Actuator mappings below this confidence are compiled as unavailable."""

READ_VERBS: Final[frozenset[str]] = frozenset(
    {"read", "get", "fetch", "poll", "peek", "query", "list", "describe", "is", "has"}
)
OPEN_VERBS: Final[frozenset[str]] = frozenset({"open", "connect"})
CLOSE_VERBS: Final[frozenset[str]] = frozenset({"close", "disconnect"})
DENY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "set", "write", "send", "move", "walk", "run", "drive", "steer", "joystick", "stick",
        "velocity", "vel", "speed", "twist", "cmd", "command", "stand", "sit", "lie", "jump",
        "flip", "dance", "limp", "damp", "torque", "freeze", "stop", "halt", "brake", "estop",
        "posture", "gait", "calibrate", "reboot", "reset", "flash", "update", "upgrade", "arm",
        "disarm", "enable", "disable", "start", "trigger", "fire", "play", "exec", "execute",
        "apply", "push", "publish", "release", "unlock", "lock", "kill", "power", "shutdown",
        "erase", "delete", "format", "motor", "servo", "actuate", "go",
    }
)  # fmt: skip
SIM_MARKERS: Final[tuple[str, ...]] = ("sim://", "mock://", "replay://", "fake://", "loopback")
_READ_ONLY_DOC = re.compile(
    r"read[- ]only|no side[- ]effects?|does not (command|actuate|move)|never affects? robot motion",
    re.IGNORECASE,
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_EXCEPTION_BASE = re.compile(r"(Error|Exception|Warning)$")


class MotionDisabledError(RuntimeError):
    """Raised whenever anything asks for real motion in this milestone."""


class ProbeClass(StrEnum):
    OPEN = "lifecycle_open"
    CLOSE = "lifecycle_close"
    ACCESSOR = "accessor"
    READ = "read"
    SKIP = "skip"


@dataclass(frozen=True)
class ProbeDecision:
    probe_class: ProbeClass
    allowed: bool
    reason: str


def name_tokens(name: str) -> list[str]:
    """Split snake_case / camelCase identifiers into lowercase tokens."""
    spaced = _CAMEL.sub("_", name)
    return [token.lower() for token in spaced.split("_") if token]


def assert_real_motion_disabled() -> None:
    """Guard used by the compiler: refuses to run if the invariant was tampered with."""
    if REAL_MOTION_ENABLED:
        raise MotionDisabledError("BODYBOOT milestone 1 must never enable real motion")


def is_exception_class(cls: ClassInfo) -> bool:
    return any(_EXCEPTION_BASE.search(base) for base in cls.bases)


def handle_class_names(classes: list[ClassInfo]) -> set[str]:
    """SDK classes that are live handles (channels, sessions) rather than data or errors.

    Only these make a zero-argument method an *accessor*. A method returning a
    plain data record must still pass the read-only test before it is called.
    """
    return {cls.name for cls in classes if not cls.is_dataclass and not is_exception_class(cls)}


def classify_probe_call(method: CallableInfo, handle_names: set[str]) -> ProbeDecision:
    """Decide whether the runtime probe may call ``method``. Deny wins over allow.

    ``handle_names`` comes from :func:`handle_class_names`.
    """
    tokens = name_tokens(method.name)
    if method.is_async:
        return ProbeDecision(ProbeClass.SKIP, False, "async callables are not probed")
    if method.required_params:
        names = ", ".join(p.name for p in method.required_params)
        return ProbeDecision(ProbeClass.SKIP, False, f"requires arguments ({names})")
    if tokens and tokens[0] in OPEN_VERBS and len(tokens) == 1:
        return ProbeDecision(ProbeClass.OPEN, True, "link lifecycle (simulated transport only)")
    if tokens and tokens[0] in CLOSE_VERBS and len(tokens) == 1:
        return ProbeDecision(ProbeClass.CLOSE, True, "link lifecycle")
    denied = [token for token in tokens if token in DENY_TOKENS]
    if denied:
        return ProbeDecision(ProbeClass.SKIP, False, f"name contains actuation/mutation token {denied[0]!r}")
    if method.is_property:
        return ProbeDecision(ProbeClass.READ, True, "public property")
    returned = (method.returns or "").strip("'\" ").rsplit(".", 1)[-1]
    if returned in handle_names:
        return ProbeDecision(ProbeClass.ACCESSOR, True, f"zero-argument accessor -> {returned}")
    if tokens and tokens[0] in READ_VERBS:
        return ProbeDecision(ProbeClass.READ, True, f"read verb {tokens[0]!r}, no arguments")
    if method.docstring and _READ_ONLY_DOC.search(method.docstring):
        return ProbeDecision(ProbeClass.READ, True, "documented as read-only, no arguments")
    return ProbeDecision(ProbeClass.SKIP, False, "not provably read-only")


def simulated_transport_proof(cls: ClassInfo, constants: dict[str, Any]) -> tuple[bool, str]:
    """Positive proof that instantiating ``cls`` with defaults selects a simulated transport."""
    init = cls.init
    if init is None:
        return False, "no constructor to inspect"
    if init.required_params:
        return False, "constructor needs arguments BODYBOOT will not invent"
    for param in init.params:
        if param.default is None:
            continue
        default = param.default.strip()
        value: Any = default.strip("'\"")
        if default in constants:
            value = constants[default]
        if isinstance(value, str) and any(marker in value.lower() for marker in SIM_MARKERS):
            return True, f"constructor default {param.name}={value!r} selects a simulated transport"
    return False, "no constructor default proves the transport is simulated"


def check_plan_safety(plan: EmbodimentPlan) -> list[PlanIssue]:
    """Safety rules on top of schema validation. Errors here block the motion surface."""
    issues: list[PlanIssue] = []
    motion = plan.get("locomotion.velocity")
    stop = plan.get("locomotion.stop")
    if motion is not None and stop is None:
        issues.append(PlanIssue("error", "locomotion.velocity", "motion is mapped but no stop capability is"))
    for cap in (motion, stop):
        if cap is not None and cap.confidence < MIN_ACTUATOR_CONFIDENCE:
            issues.append(
                PlanIssue(
                    "error",
                    cap.canonical_name,
                    f"actuator confidence {cap.confidence:.2f} is below "
                    f"{MIN_ACTUATOR_CONFIDENCE:.2f}; capability withheld",
                )
            )
    if motion is not None and stop is not None and motion.vendor_symbol == stop.vendor_symbol:
        issues.append(PlanIssue("error", "locomotion.stop", "stop and motion map to the same vendor symbol"))
    for cap in plan.capabilities:
        if cap.risk == "read_only" and cap.parameters:
            issues.append(PlanIssue("error", cap.canonical_name, "read-only capabilities take no parameters"))
    return issues


def motion_surface_allowed(plan: EmbodimentPlan) -> bool:
    """True when the (mock-only) velocity surface may be generated at all."""
    blocking = {"locomotion.velocity", "locomotion.stop"}
    return plan.get("locomotion.velocity") is not None and not any(
        issue.severity == "error" and issue.capability in blocking for issue in check_plan_safety(plan)
    )
