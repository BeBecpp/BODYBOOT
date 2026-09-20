"""Verifier: judges a run independently of whatever the agent claimed."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bodyboot.compiler.native_target import NATIVE_PACKAGE
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.plan import load_plan
from bodyboot.runs import RunPaths
from bodyboot.safety.policy import REAL_MOTION_ENABLED
from bodyboot.verification import contracts
from bodyboot.verification.contracts import GROUP_ORDER, Check, Checklist

STATUS_VERIFIED = "VERIFIED"
STATUS_FAILED = "FAILED"
STATUS_UNSCORED = "UNSCORED"


@dataclass
class GroupScore:
    title: str
    passed: int
    total: int
    unscored: int

    @property
    def status(self) -> str:
        if self.total == 0 and self.unscored:
            return "UNSCORED"
        return "PASS" if self.passed == self.total else "FAIL"


@dataclass
class VerificationResult:
    run_id: str
    status: str
    checks: list[Check] = field(default_factory=list)
    evaluator: str | None = None
    real_motion: str = "DISABLED"
    hardware_validated: bool = False
    notes: list[str] = field(default_factory=list)

    def groups(self) -> list[GroupScore]:
        scores = []
        for title in GROUP_ORDER:
            members = [c for c in self.checks if c.group == title]
            scored = [c for c in members if c.passed is not None]
            scores.append(
                GroupScore(
                    title, sum(1 for c in scored if c.passed), len(scored), len(members) - len(scored)
                )  # fmt: skip
            )
        return scores

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total(self) -> int:
        return sum(1 for c in self.checks if c.passed is not None)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.passed is False]

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "passed": self.passed,
            "total": self.total,
            "real_motion": self.real_motion,
            "hardware_validated": self.hardware_validated,
            "evaluator": self.evaluator,
            "notes": self.notes,
            "groups": [{**asdict(g), "status": g.status} for g in self.groups()],
            "checks": [asdict(c) for c in self.checks],
        }


def find_evaluator(sdk_path: Path) -> Path | None:
    """Ground truth lives NEXT TO an SDK fixture (never inside it, so it is never scanned)."""
    candidate = sdk_path.resolve().parent / "evaluator" / "expected_capabilities.json"
    return candidate if candidate.is_file() else None


def run_native_contracts(
    *, sdk_path: Path, native_dir: Path, snapshot: SdkSnapshot, expected: dict[str, Any] | None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:  # fmt: skip
    """Execute the generated adapter in a separate interpreter and return the collected facts."""
    vectors = None
    if expected:
        truth = expected["capabilities"].get("locomotion.velocity", {})
        vectors = [v["canonical"] for v in truth.get("vectors", [])]
    job = {
        "sdk_path": str(sdk_path.resolve()),
        "native_dir": str(native_dir.resolve()),
        "package": NATIVE_PACKAGE,
        "vendor_classes": [c.qualname for c in snapshot.all_classes() if not c.is_dataclass],
        "velocity_vectors": vectors or list(contracts.DEFAULT_VECTORS),
        "expected": expected,
    }
    with tempfile.TemporaryDirectory(prefix="bodyboot-verify-") as tmp:
        job_file, facts_file = Path(tmp) / "job.json", Path(tmp) / "facts.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "bodyboot.verification.native_runner", str(job_file),
                 str(facts_file)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return {"fatal": f"adapter contract runner timed out after {timeout_s:.0f}s"}
        if not facts_file.is_file():
            return {"fatal": f"runner produced no facts (exit {completed.returncode}): "
                             f"{completed.stderr[-400:]}"}  # fmt: skip
        facts: dict[str, Any] = json.loads(facts_file.read_text(encoding="utf-8"))
        return facts


def verify_run(paths: RunPaths, *, expected_path: Path | None = None) -> VerificationResult:
    """Run every contract for one run and persist ``verification.json``."""
    manifest = paths.read_manifest()
    sdk_path = Path(manifest["sdk_path"])
    snapshot = SdkSnapshot.model_validate(json.loads(paths.snapshot.read_text(encoding="utf-8")))
    plan = load_plan(paths.plan)

    evaluator = expected_path or find_evaluator(sdk_path)
    expected: dict[str, Any] | None = None
    if evaluator is not None:
        expected = json.loads(evaluator.read_text(encoding="utf-8"))

    checklist = Checklist()
    contracts.discovery_checks(checklist, plan, snapshot)
    contracts.semantic_checks(checklist, plan, expected)
    contracts.plan_checks(checklist, plan, snapshot)

    facts = run_native_contracts(sdk_path=sdk_path, native_dir=paths.native_dir, snapshot=snapshot, expected=expected)
    accessors = contracts.accessor_symbols(snapshot)
    contracts.native_checks(checklist, facts, expected, accessors)
    contracts.dimos_checks(checklist, paths.dimos_dir)

    generated_files = sorted(
        p for d in (paths.native_dir, paths.dimos_dir) if d.is_dir() for p in d.rglob("*") if p.is_file()
    )
    prompts = sorted(paths.agent_dir.glob("*prompt*.txt")) if paths.agent_dir.is_dir() else []
    contracts.safety_checks(
        checklist,
        plan=plan,
        facts=facts,
        expected=expected,
        generated_root=paths.generated_root,
        native_dir=paths.native_dir,
        dimos_dir=paths.dimos_dir,
        generated_files=generated_files,
        leak_scan_files=[*prompts, paths.plan, *generated_files],
        accessors=accessors,
    )

    checks = checklist.checks
    if any(c.passed is False for c in checks) or REAL_MOTION_ENABLED:
        status = STATUS_FAILED
    elif any(c.passed is None for c in checks):
        status = STATUS_UNSCORED
    else:
        status = STATUS_VERIFIED
    notes = [
        "Verified against the deterministic SDK fixture only. No physical robot was involved; "
        "this is NOT hardware validation.",
        "dimOS package verified statically (structure, conventions, entry points); it was not "
        "executed inside a dimOS runtime.",
    ]
    if expected is None:
        notes.append("No evaluator ground truth found next to the SDK: semantic checks unscored.")
    result = VerificationResult(
        run_id=paths.run_id,
        status=status,
        checks=checks,
        evaluator=str(evaluator) if evaluator else None,
        notes=notes,
    )
    paths.verification.write_text(json.dumps(result.to_json(), indent=2) + "\n", encoding="utf-8")
    paths.update_manifest(verification_status=status, checks_passed=result.passed,
                          checks_total=result.total)  # fmt: skip
    return result
