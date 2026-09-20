"""The independent verifier must catch wrong embodiments, and the CLI must run end to end."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bodyboot import pipeline
from bodyboot.cli import app
from bodyboot.runs import RunPaths
from bodyboot.verification.contracts import (
    G_DIMOS,
    G_DISCOVERY,
    G_NATIVE,
    G_SAFETY,
    G_SEMANTIC,
    close_enough,
)
from bodyboot.verification.verifier import VerificationResult

from .conftest import SDK, capability, make_renamed_sdk, make_run

runner = CliRunner()


def verify_tampered(det_run: RunPaths, workspace: Path, plan: dict[str, Any]) -> VerificationResult:
    paths = make_run(workspace, det_run, plan)
    pipeline.stage_compile(paths)
    return pipeline.stage_verify(paths)


def failed(result: VerificationResult, group: str) -> list[str]:
    return [c.name for c in result.failures() if c.group == group]


def test_correct_embodiment_is_verified_with_a_full_scorecard(det_run: RunPaths) -> None:
    data = json.loads(det_run.verification.read_text(encoding="utf-8"))
    assert data["status"] == "VERIFIED" and data["real_motion"] == "DISABLED"
    assert data["hardware_validated"] is False
    assert data["passed"] == data["total"] >= 44
    groups = {g["title"]: (g["passed"], g["total"]) for g in data["groups"]}
    assert groups[G_DISCOVERY] == (6, 6) and groups[G_SEMANTIC] == (6, 6)
    assert groups[G_NATIVE][1] >= 18 and groups[G_DIMOS][1] >= 12 and groups[G_SAFETY][1] >= 8
    assert any("NOT hardware validation" in note for note in data["notes"])


def test_verifier_catches_swapped_velocity_axes(det_run: RunPaths, plan_dict: dict[str, Any], tmp_path: Path) -> None:
    """A plausible-looking plan that assumes canonical argument order must FAIL."""
    params = capability(plan_dict, "locomotion.velocity")["parameters"]
    params[0]["canonical_source"], params[1]["canonical_source"] = "vx", "vy"
    result = verify_tampered(det_run, tmp_path, plan_dict)
    assert result.status == "FAILED"
    assert any("vendor-convention translation" in name for name in failed(result, G_NATIVE))
    assert failed(result, G_SEMANTIC) == [], "the symbol is right; only the executed contract can tell"


def test_verifier_catches_wrong_units_and_sign(det_run: RunPaths, plan_dict: dict[str, Any], tmp_path: Path) -> None:
    accel = next(f for f in capability(plan_dict, "imu")["fields"] if f["canonical_field"] == "accel_mps2")
    accel["scale"] = 1.0  # "forgot" that the vendor reports g
    capability(plan_dict, "locomotion.velocity")["parameters"][2]["scale"] = 57.29577951308232  # sign lost
    names = failed(verify_tampered(det_run, tmp_path, plan_dict), G_NATIVE)
    assert "imu: acceleration in m/s^2" in names and "imu: |accel| is gravity-like (8.5-11 m/s^2)" in names
    assert any("vendor-convention translation" in n for n in names)


def test_verifier_rejects_torque_cut_as_stop(det_run: RunPaths, plan_dict: dict[str, Any], tmp_path: Path) -> None:
    stop = capability(plan_dict, "locomotion.stop")
    stop["vendor_symbol"] = stop["vendor_symbol"].replace("freeze", "go_limp")
    result = verify_tampered(det_run, tmp_path, plan_dict)
    assert result.status == "FAILED"
    assert "locomotion.stop" in failed(result, G_SEMANTIC)
    assert any("torque-cut" in name for name in failed(result, G_SAFETY))


def test_verifier_scores_missing_and_hallucinated_capabilities(
    det_run: RunPaths, plan_dict: dict[str, Any], tmp_path: Path
) -> None:
    plan_dict["capabilities"] = [c for c in plan_dict["capabilities"] if c["canonical_name"] != "imu"]
    capability(plan_dict, "odometry")["vendor_symbol"] = "mysterybot.state.StateChannel.read_pose"
    result = verify_tampered(det_run, tmp_path, plan_dict)
    assert sorted(failed(result, G_DISCOVERY)) == ["imu", "odometry"]
    score = next(g for g in result.groups() if g.title == G_DISCOVERY)
    assert (score.passed, score.total, score.status) == (4, 6, "FAIL")


def test_verifier_detects_tampered_generated_code(det_run: RunPaths, plan_dict: dict[str, Any], tmp_path: Path) -> None:
    paths = make_run(tmp_path, det_run, plan_dict)
    pipeline.stage_compile(paths)
    safety = paths.native_dir / "bodyboot_native_adapter" / "safety.py"
    safety.write_text(
        safety.read_text(encoding="utf-8").replace(
            "REAL_MOTION_ENABLED = False", 'import os\nREAL_MOTION_ENABLED = os.environ.get("GO") == "1"'
        ),
        encoding="utf-8",
    )
    adapter = paths.native_dir / "bodyboot_native_adapter" / "adapter.py"
    adapter.write_text(
        adapter.read_text(encoding="utf-8").replace(
            "        self.command_log.append(command)\n        return command",
            "        getattr(self._owner(['sport']), 'joystick')(**vendor_kwargs)\n"
            "        self.command_log.append(command)\n        return command",
        ),
        encoding="utf-8",
    )
    names = failed(pipeline.stage_verify(paths), G_SAFETY)
    assert any("ZERO vendor SDK calls" in n for n in names), "spies must see the smuggled motion call"
    assert any("literal False" in n for n in names) and any("escape hatches" in n for n in names)


def test_verifier_detects_evaluator_leak(det_run: RunPaths, plan_dict: dict[str, Any], tmp_path: Path) -> None:
    plan_dict["warnings"] = ["copied from BODYBOOT-EVALUATOR-CANARY-7f3a91c2d4e8"]
    names = failed(verify_tampered(det_run, tmp_path, plan_dict), G_SAFETY)
    assert any("no evaluator answers" in n for n in names)


def test_sdk_without_ground_truth_is_unscored_never_verified(tmp_path: Path) -> None:
    sdk = tmp_path / "lonely" / "sdk"
    shutil.copytree(SDK, sdk)
    paths, _ = pipeline.stage_discover(tmp_path / "ws", sdk)
    pipeline.stage_analyze(paths, "deterministic")
    pipeline.stage_compile(paths)
    result = pipeline.stage_verify(paths)
    assert result.status == "UNSCORED" and result.failures() == []
    assert next(g for g in result.groups() if g.title == G_SEMANTIC).status == "UNSCORED"


def test_full_pipeline_generalises_to_a_renamed_sdk(tmp_path: Path) -> None:
    """Same robot, entirely different names: nothing in the toolchain may rely on the fixture."""
    sdk = make_renamed_sdk(tmp_path / "other")
    paths, _ = pipeline.stage_discover(tmp_path / "ws", sdk)
    pipeline.stage_analyze(paths, "deterministic")
    pipeline.stage_compile(paths)
    result = pipeline.stage_verify(paths)
    assert result.status == "VERIFIED", [f"{c.name}: {c.detail}" for c in result.failures()]
    adapter = (paths.native_dir / "bodyboot_native_adapter" / "adapter.py").read_text(encoding="utf-8")
    assert "acmerobot" in adapter and "mysterybot" not in adapter


def test_close_enough_semantics() -> None:
    assert close_enough([1.0, 2.0], (1.0, 2.0 + 1e-9)) and not close_enough([1.0], [1.0, 2.0])
    assert not close_enough(1, True) and close_enough({"__bytes__": 3}, {"__bytes__": 3})


def test_cli_demo_runs_the_whole_pipeline(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "--agent", "deterministic", "--workspace", str(tmp_path),
                                 "--sdk", str(SDK)])  # fmt: skip
    assert result.exit_code == 0, result.output
    for marker in ("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]", "BODYBOOT VERIFIED", "DISABLED",
                   "Give the AI a robot, not a robot driver."):  # fmt: skip
        assert marker in result.output, marker
    run_dir = next((tmp_path / ".bodyboot" / "runs").iterdir())
    for name in ("sdk_snapshot.json", "embodiment_plan.json", "verification.json", "report.html",
                 "baseline_comparison.md", "run.json"):  # fmt: skip
        assert (run_dir / name).is_file(), name
    assert (tmp_path / "generated" / f"native_{run_dir.name}").is_dir()
    assert (tmp_path / "generated" / f"dimos_{run_dir.name}").is_dir()
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    for section in ("SDK input", "Discovery graph", "Discovered capabilities", "Evidence",
                    "Generated files", "Independent verification", "<svg", "HARDWARE VALIDATED: NO"):  # fmt: skip
        assert section in html, section
    assert "<script" not in html


def test_cli_stages_work_individually_and_verify_exits_nonzero_on_failure(tmp_path: Path) -> None:
    ws = ["--workspace", str(tmp_path)]
    assert runner.invoke(app, ["discover", str(SDK), *ws]).exit_code == 0
    assert runner.invoke(app, ["analyze", "--agent", "deterministic", *ws]).exit_code == 0
    run_dir = next((tmp_path / ".bodyboot" / "runs").iterdir())
    plan_path = run_dir / "embodiment_plan.json"
    assert runner.invoke(app, ["compile", str(plan_path), *ws]).exit_code == 0
    assert runner.invoke(app, ["verify", run_dir.name, *ws]).exit_code == 0
    assert runner.invoke(app, ["report", *ws]).exit_code == 0

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    capability(plan, "imu")["fields"][0]["scale"] = 1.0
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert runner.invoke(app, ["compile", str(plan_path), *ws]).exit_code == 0
    broken = runner.invoke(app, ["verify", run_dir.name, *ws])
    assert broken.exit_code == 1 and "STATUS: FAILED" in broken.output


def test_cli_reports_errors_cleanly(tmp_path: Path) -> None:
    ws = ["--workspace", str(tmp_path)]
    assert runner.invoke(app, ["verify", *ws]).exit_code == 1
    assert runner.invoke(app, ["discover", str(tmp_path / "nothing"), *ws]).exit_code == 1
    bad = runner.invoke(app, ["demo", "--agent", "nonsense", "--sdk", str(SDK), *ws])
    assert bad.exit_code == 1 and "unknown agent backend" in bad.output


@pytest.mark.parametrize("name", ["demo.ps1", "demo.sh"])
def test_demo_scripts_exist_and_call_the_cli(name: str) -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / name).read_text(encoding="utf-8")
    assert "bodyboot demo" in text and "--agent" in text
