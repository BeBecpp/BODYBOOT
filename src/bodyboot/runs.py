"""Run directories: where every artifact of one BODYBOOT run lives.

Layout::

    <workspace>/.bodyboot/runs/<run-id>/
        run.json                 manifest (sdk path, agent, timestamps, artifacts)
        sdk_snapshot.json        discovery output
        agent/                   exact prompt, raw response, CLI help, command
        embodiment_plan.json     the agent's decision
        verification.json        independent contract results
        report.html
    <workspace>/generated/native_<run-id>/
    <workspace>/generated/dimos_<run-id>/
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


class RunError(RuntimeError):
    """A run directory is missing or malformed."""


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def find_workspace(start: Path | None = None) -> Path:
    """Nearest ancestor holding a BODYBOOT checkout, else the starting directory."""
    origin = (start or Path.cwd()).resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / "fixtures" / "mystery_go2_sdk").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return origin


def default_fixture_sdk(workspace: Path) -> Path:
    return workspace / "fixtures" / "mystery_go2_sdk"


@dataclass(frozen=True)
class RunPaths:
    """All well-known paths of one run."""

    workspace: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.workspace / ".bodyboot" / "runs" / self.run_id

    @property
    def manifest(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def snapshot(self) -> Path:
        return self.run_dir / "sdk_snapshot.json"

    @property
    def agent_dir(self) -> Path:
        return self.run_dir / "agent"

    @property
    def plan(self) -> Path:
        return self.run_dir / "embodiment_plan.json"

    @property
    def verification(self) -> Path:
        return self.run_dir / "verification.json"

    @property
    def report(self) -> Path:
        return self.run_dir / "report.html"

    @property
    def baseline(self) -> Path:
        return self.run_dir / "baseline_comparison.md"

    @property
    def generated_root(self) -> Path:
        return self.workspace / "generated"

    @property
    def native_dir(self) -> Path:
        return self.generated_root / f"native_{self.run_id}"

    @property
    def dimos_dir(self) -> Path:
        return self.generated_root / f"dimos_{self.run_id}"

    def ensure(self) -> RunPaths:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self

    def read_manifest(self) -> dict[str, Any]:
        if not self.manifest.is_file():
            raise RunError(f"run {self.run_id!r} not found under {self.run_dir.parent}")
        data: dict[str, Any] = json.loads(self.manifest.read_text(encoding="utf-8"))
        return data

    def update_manifest(self, **fields: Any) -> dict[str, Any]:
        data = self.read_manifest() if self.manifest.is_file() else {"run_id": self.run_id}
        data.update(fields)
        self.manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return data


def open_run(workspace: Path, run_id: str) -> RunPaths:
    if not RUN_ID_PATTERN.match(run_id):
        raise RunError(f"{run_id!r} is not a valid run id")
    paths = RunPaths(workspace, run_id)
    paths.read_manifest()
    return paths


def resolve_run_id(workspace: Path, run_id: str | None) -> str:
    """Accept an explicit id, or 'latest'/None for the newest run."""
    if run_id and run_id != "latest":
        return run_id
    runs_dir = workspace / ".bodyboot" / "runs"
    candidates = sorted(p.name for p in runs_dir.iterdir() if RUN_ID_PATTERN.match(p.name)) if runs_dir.is_dir() else []
    if not candidates:
        raise RunError("no runs found - run `bodyboot discover <sdk-path>` first")
    return candidates[-1]


def run_id_from_plan_path(plan_path: Path) -> str | None:
    """A plan stored inside a run directory belongs to that run."""
    parent = plan_path.resolve().parent.name
    return parent if RUN_ID_PATTERN.match(parent) else None
