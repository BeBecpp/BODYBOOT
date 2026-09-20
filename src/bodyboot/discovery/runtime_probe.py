"""Read-only runtime probe (parent side).

The vendor SDK is imported and exercised in a *separate interpreter* so that a
misbehaving SDK cannot hang or pollute BODYBOOT, and so that two SDKs with the
same package name never collide in ``sys.modules``. What may be called is
decided by ``bodyboot.safety.policy`` - never by this module.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from bodyboot.discovery.models import RuntimeProbeReport, SdkSnapshot


def run_runtime_probe(sdk_path: Path, snapshot: SdkSnapshot, *, timeout_s: float = 30.0) -> RuntimeProbeReport:
    """Run the policy-gated probe in a subprocess and return its report."""
    with tempfile.TemporaryDirectory(prefix="bodyboot-probe-") as tmp:
        snapshot_file = Path(tmp) / "static_snapshot.json"
        report_file = Path(tmp) / "probe_report.json"
        snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "bodyboot.discovery.probe_worker",
            str(sdk_path.resolve()),
            str(snapshot_file),
            str(report_file),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RuntimeProbeReport(executed=False, reason=f"runtime probe timed out after {timeout_s:.0f}s")
        if completed.returncode != 0 or not report_file.is_file():
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
            return RuntimeProbeReport(
                executed=False,
                reason=f"runtime probe failed (exit {completed.returncode}): {' | '.join(tail)}",
            )
        return RuntimeProbeReport.model_validate(json.loads(report_file.read_text(encoding="utf-8")))
