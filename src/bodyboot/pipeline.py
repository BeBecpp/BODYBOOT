"""Pipeline stages: discover -> analyze -> compile -> verify -> report.

Each stage reads and writes files in the run directory, so every stage can also
be driven on its own from the CLI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from bodyboot.agents import AgentBackend, AgentResult, get_backend
from bodyboot.compiler import CompileResult, compile_plan
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.discovery.scanner import discover
from bodyboot.plan import dump_plan, load_plan
from bodyboot.runs import RunPaths, new_run_id
from bodyboot.verification.verifier import VerificationResult, verify_run


def stage_discover(
    workspace: Path, sdk_path: Path, *, run_id: str | None = None, runtime: bool = True
) -> tuple[RunPaths, SdkSnapshot]:
    paths = RunPaths(workspace, run_id or new_run_id()).ensure()
    snapshot = discover(sdk_path, runtime=runtime)
    paths.snapshot.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
    paths.update_manifest(
        sdk_path=str(sdk_path.resolve()),
        sdk_name=snapshot.sdk_name,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stats=snapshot.stats.model_dump(),
    )
    return paths, snapshot


def load_snapshot(paths: RunPaths) -> SdkSnapshot:
    return SdkSnapshot.model_validate(json.loads(paths.snapshot.read_text(encoding="utf-8")))


def stage_analyze(paths: RunPaths, agent: str, *, backend: AgentBackend | None = None) -> AgentResult:
    backend = backend or get_backend(agent)
    result = backend.analyze(load_snapshot(paths), paths.agent_dir)
    paths.plan.write_text(dump_plan(result.plan), encoding="utf-8")
    paths.update_manifest(
        agent=result.backend,
        agent_model=result.model,
        agent_attempts=result.attempts,
        agent_duration_s=round(result.duration_s, 2),
        agent_cost_usd=result.cost_usd,
    )
    return result


def stage_compile(paths: RunPaths, plan_path: Path | None = None) -> CompileResult:
    plan = load_plan(plan_path or paths.plan)
    manifest = paths.read_manifest()
    result = compile_plan(
        plan,
        run_id=paths.run_id,
        generated_root=paths.generated_root,
        agent_backend=str(manifest.get("agent", "unknown")),
    )
    paths.update_manifest(
        generated_files=result.relative_files(paths.workspace),
        withheld=result.withheld,
        plan_issues=[issue.render() for issue in result.issues],
    )
    return result


def stage_verify(paths: RunPaths, *, expected_path: Path | None = None) -> VerificationResult:
    return verify_run(paths, expected_path=expected_path)


def stage_report(paths: RunPaths) -> Path:
    from bodyboot.verification.report import write_report

    return write_report(paths)
