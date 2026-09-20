"""Compiler output, and the generated native adapter actually running against the fixture."""

from __future__ import annotations

import ast
import json
import math
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from bodyboot.compiler import CompileError, compile_plan
from bodyboot.plan import parse_plan
from bodyboot.runs import RunPaths

from .conftest import REPO, SDK, capability, run_python

ADAPTER_SCRIPT = """
import dataclasses, json
from bodyboot_native_adapter import GeneratedRobotAdapter
robot = GeneratedRobotAdapter()
robot.connect()
imu, odom, frame, health = robot.imu(), robot.odometry(), robot.camera_rgb(), robot.health()
command = robot.velocity(0.3, 0.1, 0.5)
vendor_log_after_velocity = list(robot._session.sport()._log)
robot.stop()
print(json.dumps({
    "imu": dataclasses.asdict(imu), "odom": dataclasses.asdict(odom),
    "frame": [frame.width, frame.height, frame.encoding, len(frame.data), frame.stamp_s, frame.frame],
    "health": dataclasses.asdict(health), "command": dataclasses.asdict(command),
    "vendor_log_after_velocity": vendor_log_after_velocity,
    "vendor_log_after_stop": list(robot._session.sport()._log),
    "log_len": len(robot.command_log),
}))
"""


def test_compiler_writes_both_targets_at_runtime(det_run: RunPaths) -> None:
    native = det_run.native_dir / "bodyboot_native_adapter"
    for name in ("__init__.py", "adapter.py", "safety.py", "types.py"):
        assert (native / name).is_file()
    package = det_run.dimos_dir / "src" / "bodyboot_generated_robot"
    for name in ("connection_module.py", "skill_container.py", "blueprints.py", "connection_spec.py"):
        assert (package / name).is_file()
    assert (det_run.dimos_dir / "pyproject.toml").is_file()
    manifest = json.loads((det_run.native_dir / "embodiment_manifest.json").read_text(encoding="utf-8"))
    assert manifest["real_motion_enabled"] is False and manifest["hardware_validated"] is False
    assert manifest["run_id"] == det_run.run_id


def test_generated_python_is_valid_and_carries_the_plan(det_run: RunPaths) -> None:
    for path in [*det_run.native_dir.rglob("*.py"), *det_run.dimos_dir.rglob("*.py")]:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    adapter = (det_run.native_dir / "bodyboot_native_adapter" / "adapter.py").read_text(encoding="utf-8")
    assert "-57.29577951308232" in adapter and "[1, 2, 3, 0]" in adapter and "9.80665" in adapter
    assert "MOCK-ONLY" in adapter and "NOT been" in adapter


def test_compiler_and_verifier_sources_know_nothing_about_the_fixture() -> None:
    """The toolchain must not depend on the fixture's names (or on the evaluator's answers)."""
    fixture_names = re.compile(
        r"mysterybot|VideoPipe|StateChannel|SportChannel|RobotSession|fetch_rgb|read_imu_packet|"
        r"read_packet|go_limp|accel_g|quat_wxyz|heading_deg|rtt_ms"
    )
    offenders = [
        f"{path.relative_to(REPO)}: {match.group(0)}"
        for path in (REPO / "src" / "bodyboot").rglob("*")
        if path.is_file() and path.suffix in (".py", ".j2")
        if (match := fixture_names.search(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []
    # Only the verifier may know the evaluator file; agents mention it solely as a forbidden marker.
    for package in ("agents", "compiler", "discovery"):
        for path in (REPO / "src" / "bodyboot" / package).rglob("*"):
            if path.is_file() and path.name != "prompting.py" and path.suffix in (".py", ".j2"):
                assert "expected_capabilities" not in path.read_text(encoding="utf-8"), path


def test_compile_is_deterministic(plan_dict: dict[str, Any], tmp_path: Path) -> None:
    plan = parse_plan(plan_dict)
    first = compile_plan(plan, run_id="20260101-000000-aaaa", generated_root=tmp_path / "a")
    second = compile_plan(plan, run_id="20260101-000000-aaaa", generated_root=tmp_path / "b")
    a = {p.relative_to(tmp_path / "a"): p.read_bytes() for p in first.files}
    b = {p.relative_to(tmp_path / "b"): p.read_bytes() for p in second.files}
    assert a == b and len(a) >= 14


def test_native_adapter_runs_against_the_fixture_with_si_units(det_run: RunPaths) -> None:
    out = run_python(ADAPTER_SCRIPT, SDK, det_run.native_dir)
    assert math.sqrt(sum(a * a for a in out["imu"]["accel_mps2"])) == pytest.approx(9.80, abs=0.05)
    assert out["imu"]["gyro_rps"][2] == pytest.approx(math.radians(15.1))
    x, y, z, w = out["imu"]["orientation_xyzw"]
    assert (x, y) == (0.0, 0.0) and z * z + w * w == pytest.approx(1.0, abs=1e-5)
    assert out["odom"]["yaw_rad"] == pytest.approx(math.radians(179.7))
    assert out["odom"]["position_m"] == pytest.approx([0.004, -0.001, 0.31])
    assert out["odom"]["yaw_rate_rps"] == pytest.approx(math.radians(15.0))
    assert out["frame"] == [160, 90, "rgb8", 160 * 90 * 3, pytest.approx(0.033333), "camera_optical"]
    assert out["health"] == {"alive": True, "latency_s": pytest.approx(0.00375)}


def test_velocity_is_translated_but_never_sent(det_run: RunPaths) -> None:
    out = run_python(ADAPTER_SCRIPT, SDK, det_run.native_dir)
    command = out["command"]
    assert command["vendor_kwargs"] == {
        "v1": pytest.approx(0.1), "v2": pytest.approx(0.3), "v3": pytest.approx(-28.6478897565),
    }  # fmt: skip
    assert command["forwarded_to_vendor"] is False and command["clamped"] is False
    assert out["vendor_log_after_velocity"] == [], "the vendor SDK saw a motion command"


def test_stop_maps_to_the_safe_vendor_stop(det_run: RunPaths) -> None:
    out = run_python(ADAPTER_SCRIPT, SDK, det_run.native_dir)
    assert out["vendor_log_after_stop"] == ["freeze()"]
    assert out["log_len"] == 2


def test_motion_denial_cannot_be_bypassed(det_run: RunPaths) -> None:
    script = """
import json, os
import bodyboot_native_adapter as pkg
results = {"flag": pkg.REAL_MOTION_ENABLED}
for label, action in (("ctor", lambda: pkg.GeneratedRobotAdapter(allow_real_motion=True)),
                      ("method", lambda: pkg.GeneratedRobotAdapter().enable_real_motion())):
    try:
        action(); results[label] = "ALLOWED"
    except pkg.MotionDisabledError:
        results[label] = "refused"
robot = pkg.GeneratedRobotAdapter(); robot.connect()
for label, args in (("nan", (float("nan"), 0, 0)), ("inf", (0, float("inf"), 0))):
    try:
        robot.velocity(*args); results[label] = "accepted"
    except ValueError:
        results[label] = "rejected"
big = robot.velocity(99.0, -99.0, 99.0)
results["clamped"] = [big.clamped, big.vendor_kwargs]
results["vendor_log"] = list(robot._session.sport()._log)
print(json.dumps(results))
"""
    out = run_python(script, SDK, det_run.native_dir,
                     env={"BODYBOOT_ENABLE_REAL_MOTION": "1", "REAL_MOTION_ENABLED": "true"})  # fmt: skip
    assert out["flag"] is False and out["ctor"] == out["method"] == "refused"
    assert out["nan"] == out["inf"] == "rejected"
    assert out["clamped"] == [True, {"v1": -0.4, "v2": 1.2, "v3": -100.0}]
    assert out["vendor_log"] == []


def test_missing_stop_withholds_velocity_in_generated_code(plan_dict: dict[str, Any], tmp_path: Path) -> None:
    plan_dict["capabilities"] = [
        c for c in plan_dict["capabilities"] if c["canonical_name"] not in ("locomotion.stop", "imu")
    ]
    result = compile_plan(parse_plan(plan_dict), run_id="20260101-000000-bbbb", generated_root=tmp_path)
    assert set(result.withheld) == {"imu", "locomotion.stop", "locomotion.velocity"}
    script = """
import json
import bodyboot_native_adapter as pkg
robot = pkg.GeneratedRobotAdapter(); robot.connect()
out = {"odom_ok": robot.odometry().frame}
for name, call in (("velocity", lambda: robot.velocity(0.1, 0, 0)), ("stop", robot.stop), ("imu", robot.imu)):
    try:
        call(); out[name] = "worked"
    except pkg.CapabilityUnavailableError as exc:
        out[name] = str(exc)
print(json.dumps(out))
"""
    out = run_python(script, SDK, result.native_dir)
    assert out["odom_ok"] == "odom"
    assert out["velocity"] == "locomotion.velocity: motion is mapped but no stop capability is"
    assert "not mapped" in out["stop"] and "not mapped" in out["imu"]


def test_agent_free_text_cannot_break_out_of_generated_source(plan_dict: dict[str, Any], tmp_path: Path) -> None:
    plan_dict["robot_family"] = 'dog"""\nimport os; os.system("calc")\n"""'
    capability(plan_dict, "odometry")["frame"] = "odom'; import os #"
    result = compile_plan(parse_plan(plan_dict), run_id="20260101-000000-cccc", generated_root=tmp_path)
    for path in result.files:
        if path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            assert "os" not in imported, path


def test_plan_with_nothing_usable_does_not_compile(plan_dict: dict[str, Any], tmp_path: Path) -> None:
    plan_dict["capabilities"] = []
    with pytest.raises(CompileError):
        compile_plan(parse_plan(plan_dict), run_id="20260101-000000-dddd", generated_root=tmp_path)


def test_dimos_package_structure_and_entry_points(det_run: RunPaths) -> None:
    pyproject = tomllib.loads((det_run.dimos_dir / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = pyproject["project"]["entry-points"]["dimos.blueprints"]
    assert entry_points == {
        "robot": "bodyboot_generated_robot.blueprints:bodyboot_robot",
        "connection": "bodyboot_generated_robot.connection_module:BodybootConnection",
        "skills": "bodyboot_generated_robot.skill_container:BodybootSkillContainer",
    }
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) for name in entry_points)
    assert "dimos>=0.0.14" in pyproject["project"]["dependencies"]
    assert pyproject["project"]["requires-python"] == ">=3.10,<3.13"
    assert not list(det_run.dimos_dir.rglob("all_blueprints.py"))


def test_dimos_modules_follow_dimos_conventions(det_run: RunPaths) -> None:
    package = det_run.dimos_dir / "src" / "bodyboot_generated_robot"
    connection = (package / "connection_module.py").read_text(encoding="utf-8")
    for line in ("cmd_vel: In[Twist]", "color_image: Out[Image]", "odom: Out[PoseStamped]", "imu: Out[Imu]",
                 "from dimos.core.module import Module, ModuleConfig", "from dimos.core.core import rpc"):  # fmt: skip
        assert line in connection, line
    tree = ast.parse((package / "skill_container.py").read_text(encoding="utf-8"))
    skills = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and any(ast.unparse(d) == "skill" for d in n.decorator_list)
    ]  # fmt: skip
    assert {s.name for s in skills} == {"robot_health", "describe_embodiment", "stop_robot", "move_velocity"}
    for node in skills:
        assert ast.get_docstring(node) and ast.unparse(node.returns) == "str"  # type: ignore[arg-type]
        assert all(a.annotation is not None for a in node.args.args if a.arg != "self")
    move = next(s for s in skills if s.name == "move_velocity")
    assert "MOCK-ONLY" in (ast.get_docstring(move) or "") and "DISABLED" in ast.unparse(move)
    blueprint = (package / "blueprints.py").read_text(encoding="utf-8")
    assert "autoconnect(" in blueprint and "lambda" not in blueprint
