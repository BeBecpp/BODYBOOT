"""Shared fixtures. Generated code always runs in a separate interpreter (see run_python)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bodyboot import pipeline
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.discovery.scanner import discover
from bodyboot.plan import parse_plan
from bodyboot.runs import RunPaths, new_run_id

REPO = Path(__file__).resolve().parents[1]
SDK = REPO / "fixtures" / "mystery_go2_sdk"
EVALUATOR = REPO / "fixtures" / "evaluator" / "expected_capabilities.json"

# A second, differently-named SDK derived from the fixture. If BODYBOOT only worked because
# something memorised the fixture's names, every test using this would fail.
RENAMES: tuple[tuple[str, str], ...] = (
    (r"mysterybot", "acmerobot"),
    (r"RobotSession", "BotLink"),
    (r"VideoPipe", "OpticFeed"),
    (r"StateChannel", "TelemetryPort"),
    (r"SportChannel", "GaitPort"),
    (r"StatePacket", "BodyState"),
    (r"ImuPacket", "InertialSample"),
    (r"LinkReport", "PingResult"),
    (r"\bFrame\b", "Picture"),
    (r"fetch_rgb", "grab_color"),
    (r"fetch_mono", "grab_gray"),
    (r"read_imu_packet", "get_inertial"),
    (r"read_packet", "get_body_state"),
    (r"joystick", "walk_cmd"),
    (r"\bfreeze\b", "halt"),
    (r"go_limp", "relax_joints"),
    (r"heartbeat", "ping_link"),
    (r"def video\(", "def optics("),
    (r"def state\(", "def telemetry("),
    (r"def sport\(", "def gait("),
    (r"\.video\(\)", ".optics()"),
    (r"\.state\(\)", ".telemetry()"),
    (r'\["video"\]', '["optics"]'),
    (r'\["state"\]', '["telemetry"]'),
    (r'\["sport"\]', '["gait"]'),
    (r"\bcols\b", "width_px"),
    (r"\brows\b", "height_px"),
    (r"\bbuf\b", "pixels"),
    (r"\bt_us\b", "time_us"),
    (r"accel_g", "acc_g"),
    (r"gyro_dps", "omega_dps"),
    (r"quat_wxyz", "attitude_wxyz"),
    (r"\bpos\b", "position"),
    (r"heading_deg", "yaw_deg"),
    (r"body_vel", "velocity_mps"),
    (r"turn_rate_dps", "yaw_rate_dps"),
    (r"link_up", "connected"),
    (r"rtt_ms", "latency_ms"),
    (r"\bv1\b", "ch1"),
    (r"\bv2\b", "ch2"),
    (r"\bv3\b", "ch3"),
)


def _rename(text: str) -> str:
    for pattern, replacement in RENAMES:
        text = re.sub(pattern, replacement, text)
    return text


def make_renamed_sdk(root: Path) -> Path:
    """Copy the fixture under new names (package, classes, methods, fields, parameters)."""
    sdk = root / "acme_sdk"
    package = sdk / "acmerobot"
    package.mkdir(parents=True)
    for source in (SDK / "mysterybot").glob("*.py"):
        (package / source.name).write_text(_rename(source.read_text(encoding="utf-8")), encoding="utf-8")
    (sdk / "README.md").write_text(_rename((SDK / "README.md").read_text(encoding="utf-8")), encoding="utf-8")
    evaluator = root / "evaluator"
    evaluator.mkdir()
    (evaluator / "expected_capabilities.json").write_text(
        _rename(EVALUATOR.read_text(encoding="utf-8")).replace(
            "BODYBOOT-EVALUATOR-CANARY-7f3a91c2d4e8", "BODYBOOT-EVALUATOR-CANARY-renamed0001"
        ),
        encoding="utf-8",
    )
    return sdk


def run_python(code: str, *paths: Path, env: dict[str, str] | None = None) -> Any:
    """Run ``code`` in a fresh interpreter with ``paths`` importable; returns its JSON stdout."""
    full_env = {**os.environ, **(env or {})}
    full_env["PYTHONPATH"] = os.pathsep.join(str(p) for p in paths)
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=full_env, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def make_run(workspace: Path, source: RunPaths, plan: dict[str, Any]) -> RunPaths:
    """A new run that reuses ``source``'s snapshot but carries a (possibly tampered) plan."""
    paths = RunPaths(workspace, new_run_id()).ensure()
    shutil.copy(source.snapshot, paths.snapshot)
    manifest = source.read_manifest()
    paths.update_manifest(sdk_path=manifest["sdk_path"], agent="test")
    paths.plan.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return paths


@pytest.fixture(scope="session")
def snapshot() -> SdkSnapshot:
    return discover(SDK)


@pytest.fixture(scope="session")
def det_run(tmp_path_factory: pytest.TempPathFactory) -> RunPaths:
    """One full deterministic pipeline run, shared by read-only tests."""
    workspace = tmp_path_factory.mktemp("workspace")
    paths, _ = pipeline.stage_discover(workspace, SDK)
    pipeline.stage_analyze(paths, "deterministic")
    pipeline.stage_compile(paths)
    pipeline.stage_verify(paths)
    pipeline.stage_report(paths)
    return paths


@pytest.fixture()
def plan_dict(det_run: RunPaths) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(det_run.plan.read_text(encoding="utf-8"))
    parse_plan(data)
    return data


def capability(plan: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in plan["capabilities"] if c["canonical_name"] == name)
