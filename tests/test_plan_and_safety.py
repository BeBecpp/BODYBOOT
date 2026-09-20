"""Embodiment plan schema, semantic validation and the safety policy."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from bodyboot.plan import InvalidPlanError, dump_plan, errors_of, parse_plan, validate_plan
from bodyboot.safety import policy
from bodyboot.safety.policy import check_plan_safety, motion_surface_allowed

from .conftest import capability


def test_valid_plan_roundtrips_without_issues(plan_dict: dict[str, Any]) -> None:
    plan = parse_plan(plan_dict)
    assert len(plan.capabilities) == 6
    assert errors_of(validate_plan(plan)) == [] and check_plan_safety(plan) == []
    assert parse_plan(__import__("json").loads(dump_plan(plan))) == plan


@pytest.mark.parametrize(
    "payload",
    ["just text", [], {"robot_family": "x"}, {"capabilities": "nope"}],
)
def test_structurally_invalid_plans_are_rejected(payload: Any) -> None:
    with pytest.raises(InvalidPlanError):
        parse_plan(payload)


@pytest.mark.parametrize(
    "evil",
    ["os.system('x')", "a b", "__class__", "x;import os", "sport()", "lambda", "", "_private"],
)
def test_plan_identifiers_cannot_carry_code(plan_dict: dict[str, Any], evil: str) -> None:
    """Everything that reaches generated source must be a plain public identifier."""
    for mutate in (
        lambda p: capability(p, "imu").__setitem__("vendor_symbol", f"pkg.{evil}"),
        lambda p: capability(p, "imu").__setitem__("access_path", [evil]),
        lambda p: capability(p, "imu")["fields"][0].__setitem__("vendor_paths", [evil]),
        lambda p: capability(p, "locomotion.velocity")["parameters"][0].__setitem__("vendor_param", evil),
        lambda p: p["connection"].__setitem__("open_method", evil),
    ):
        tampered = copy.deepcopy(plan_dict)
        mutate(tampered)
        with pytest.raises(InvalidPlanError):
            parse_plan(tampered)


def test_non_finite_numbers_and_missing_evidence_are_rejected(plan_dict: dict[str, Any]) -> None:
    bad_scale = copy.deepcopy(plan_dict)
    capability(bad_scale, "locomotion.velocity")["parameters"][2]["scale"] = float("inf")
    no_evidence = copy.deepcopy(plan_dict)
    capability(no_evidence, "imu")["evidence"] = []
    bad_confidence = copy.deepcopy(plan_dict)
    capability(bad_confidence, "imu")["confidence"] = 1.5
    for tampered in (bad_scale, no_evidence, bad_confidence):
        with pytest.raises(InvalidPlanError):
            parse_plan(tampered)


def test_missing_capability_is_reported_not_raised(plan_dict: dict[str, Any]) -> None:
    plan_dict["capabilities"] = [c for c in plan_dict["capabilities"] if c["canonical_name"] != "imu"]
    issues = errors_of(validate_plan(parse_plan(plan_dict)))
    assert [(i.capability, i.message) for i in issues] == [("imu", "required capability is missing")]


def test_low_confidence_produces_a_warning(plan_dict: dict[str, Any]) -> None:
    capability(plan_dict, "odometry")["confidence"] = 0.41
    issues = validate_plan(parse_plan(plan_dict))
    assert any(i.severity == "warning" and i.capability == "odometry" and "low confidence" in i.message
               for i in issues)  # fmt: skip
    assert errors_of(issues) == []


def test_wrong_risk_class_is_an_error(plan_dict: dict[str, Any]) -> None:
    capability(plan_dict, "locomotion.velocity")["risk"] = "read_only"
    messages = [i.message for i in errors_of(validate_plan(parse_plan(plan_dict)))]
    assert any("risk must be 'motion'" in m for m in messages)


def test_motion_arguments_must_each_be_mapped_exactly_once(plan_dict: dict[str, Any]) -> None:
    params = capability(plan_dict, "locomotion.velocity")["parameters"]
    params[0]["canonical_source"] = "vx"  # vx twice, vy never
    messages = [i.message for i in errors_of(validate_plan(parse_plan(plan_dict)))]
    assert any("vy must feed exactly 1" in m for m in messages)
    assert any("vx must feed exactly 1" in m for m in messages)


def test_field_mapping_shape_rules(plan_dict: dict[str, Any]) -> None:
    imu = capability(plan_dict, "imu")
    quat = next(f for f in imu["fields"] if f["canonical_field"] == "orientation_xyzw")
    quat["indices"] = [1, 2, 3]  # vec4 needs four
    imu["fields"] = [f for f in imu["fields"] if f["canonical_field"] != "gyro_rps"]
    camera = capability(plan_dict, "camera.rgb")
    next(f for f in camera["fields"] if f["canonical_field"] == "encoding")["constant"] = "bgr8"
    messages = " | ".join(i.message for i in errors_of(validate_plan(parse_plan(plan_dict))))
    assert "indices must have 4 entries" in messages
    assert "gyro_rps is not mapped" in messages
    assert "'bgr8' not in ['rgb8']" in messages


def test_real_motion_is_a_constant_false_and_guard_trips_if_tampered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert policy.REAL_MOTION_ENABLED is False
    policy.assert_real_motion_disabled()
    monkeypatch.setattr(policy, "REAL_MOTION_ENABLED", True)
    with pytest.raises(policy.MotionDisabledError):
        policy.assert_real_motion_disabled()


def test_no_stop_means_no_motion_surface(plan_dict: dict[str, Any]) -> None:
    plan_dict["capabilities"] = [c for c in plan_dict["capabilities"] if c["canonical_name"] != "locomotion.stop"]
    plan = parse_plan(plan_dict)
    assert motion_surface_allowed(plan) is False
    assert any("no stop capability" in i.message for i in check_plan_safety(plan))


def test_untrusted_actuators_and_aliased_stop_are_withheld(plan_dict: dict[str, Any]) -> None:
    shaky = copy.deepcopy(plan_dict)
    capability(shaky, "locomotion.stop")["confidence"] = 0.3
    assert motion_surface_allowed(parse_plan(shaky)) is False

    aliased = copy.deepcopy(plan_dict)
    motion_symbol = capability(aliased, "locomotion.velocity")["vendor_symbol"]
    capability(aliased, "locomotion.stop")["vendor_symbol"] = motion_symbol
    assert any("same vendor symbol" in i.message for i in check_plan_safety(parse_plan(aliased)))
