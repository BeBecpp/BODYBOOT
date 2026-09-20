"""Safety policy: read-only probing, simulated transports only, real motion disabled."""

from bodyboot.safety.policy import (
    REAL_MOTION_ENABLED,
    MotionDisabledError,
    ProbeClass,
    ProbeDecision,
    assert_real_motion_disabled,
    check_plan_safety,
    classify_probe_call,
    motion_surface_allowed,
    simulated_transport_proof,
)

__all__ = [
    "REAL_MOTION_ENABLED",
    "MotionDisabledError",
    "ProbeClass",
    "ProbeDecision",
    "assert_real_motion_disabled",
    "check_plan_safety",
    "classify_probe_call",
    "motion_surface_allowed",
    "simulated_transport_proof",
]
