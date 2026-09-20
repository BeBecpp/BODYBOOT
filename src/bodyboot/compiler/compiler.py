"""Deterministic embodiment compiler: plan in, source code out.

No model is involved here. Templates receive only values that the plan schema
validated as identifiers / numbers, or that were rendered through ``repr``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from bodyboot import __version__
from bodyboot.canonical import CAPABILITIES, VECTOR_SIZES
from bodyboot.plan import (
    CapabilityMapping,
    EmbodimentPlan,
    PlanIssue,
    dump_plan,
    errors_of,
    validate_plan,
)
from bodyboot.safety.policy import (
    assert_real_motion_disabled,
    check_plan_safety,
    motion_surface_allowed,
)

_UNSAFE_TEXT = re.compile(r"[^A-Za-z0-9 _.,()/+-]")


class CompileError(RuntimeError):
    """The plan cannot be compiled."""


@dataclass
class CompileResult:
    run_id: str
    native_dir: Path
    dimos_dir: Path
    files: list[Path] = field(default_factory=list)
    issues: list[PlanIssue] = field(default_factory=list)
    withheld: dict[str, str] = field(default_factory=dict)

    def relative_files(self, root: Path) -> list[str]:
        return sorted(path.relative_to(root).as_posix() for path in self.files)


def safe_text(value: object) -> str:
    """Free text from an agent, reduced to characters that are inert inside source files."""
    return _UNSAFE_TEXT.sub("", str(value))[:80]


def make_environment() -> Environment:
    env = Environment(
        loader=PackageLoader("bodyboot.compiler", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["py"] = repr
    env.filters["safe_text"] = safe_text
    return env


def plan_sha256(plan: EmbodimentPlan) -> str:
    return hashlib.sha256(dump_plan(plan).encode("utf-8")).hexdigest()


def usable_capabilities(
    plan: EmbodimentPlan, issues: list[PlanIssue]
) -> tuple[dict[str, CapabilityMapping], dict[str, str]]:
    """Capabilities that compile into working code, and why the others are withheld."""
    blocked: dict[str, str] = {}
    for issue in errors_of(issues):
        if issue.capability:
            blocked.setdefault(issue.capability, issue.message)
    usable: dict[str, CapabilityMapping] = {}
    withheld: dict[str, str] = {}
    for spec in CAPABILITIES:
        mapping = plan.get(spec.name)
        if mapping is None:
            withheld[spec.name] = "not mapped by the embodiment plan"
        elif spec.name in blocked:
            withheld[spec.name] = blocked[spec.name]
        else:
            usable[spec.name] = mapping
    if "locomotion.velocity" in usable and not motion_surface_allowed(plan):
        withheld["locomotion.velocity"] = "withheld by safety policy (no trustworthy stop)"
        del usable["locomotion.velocity"]
    if "locomotion.velocity" in usable and "locomotion.stop" not in usable:
        withheld["locomotion.velocity"] = "withheld: no usable stop capability"
        del usable["locomotion.velocity"]
    return usable, withheld


def sensor_context(usable: dict[str, CapabilityMapping], withheld: dict[str, str]) -> list[dict[str, Any]]:
    """Template context for every read-only capability."""
    context = []
    for spec in CAPABILITIES:
        if spec.risk != "read_only":
            continue
        mapping = usable.get(spec.name)
        fields = []
        if mapping is not None:
            for fld in spec.fields:
                source = mapping.field_source(fld.name)
                assert source is not None  # guaranteed by validate_plan
                fields.append(
                    {
                        "name": fld.name,
                        "shape": fld.shape,
                        "size": VECTOR_SIZES.get(fld.shape, 1),
                        "paths": list(source.vendor_paths),
                        "indices": source.indices,
                        "index": source.indices[0] if source.indices else None,
                        "scale": float(source.scale),
                        "constant": source.constant,
                    }
                )
        context.append(
            {
                "method": spec.adapter_method,
                "output_type": spec.output_type,
                "description": spec.description,
                "vendor_symbol": mapping.vendor_symbol if mapping else None,
                "mapping": mapping,
                "fields": fields,
                "has_frame": spec.frame_hint is not None,
                "frame": safe_text((mapping.frame if mapping else None) or spec.frame_hint or ""),
                "unavailable_reason": f"{spec.name}: {withheld.get(spec.name, '')}",
            }
        )
    return context


def compile_plan(
    plan: EmbodimentPlan,
    *,
    run_id: str,
    generated_root: Path,
    agent_backend: str = "unknown",
) -> CompileResult:
    """Generate the native adapter and the external dimOS package for ``plan``."""
    from bodyboot.compiler.dimos_target import render_dimos
    from bodyboot.compiler.native_target import render_native

    assert_real_motion_disabled()
    issues = [*validate_plan(plan), *check_plan_safety(plan)]
    usable, withheld = usable_capabilities(plan, issues)
    if not any(spec.risk == "read_only" and spec.name in usable for spec in CAPABILITIES):
        raise CompileError("the plan maps no usable capability - nothing to compile")

    native_dir = generated_root / f"native_{run_id}"
    dimos_dir = generated_root / f"dimos_{run_id}"
    for directory in (native_dir, dimos_dir):
        if directory.exists():
            shutil.rmtree(directory)
    env = make_environment()
    shared = {
        "plan": plan,
        "run_id": run_id,
        "generator_version": __version__,
        "agent_backend": safe_text(agent_backend),
        "plan_sha256": plan_sha256(plan),
        "sensors": sensor_context(usable, withheld),
        "motion": usable.get("locomotion.velocity"),
        "stop": usable.get("locomotion.stop"),
        "motion_unavailable_reason": "locomotion.velocity: " + withheld.get("locomotion.velocity", ""),
        "stop_unavailable_reason": "locomotion.stop: " + withheld.get("locomotion.stop", ""),
    }
    result = CompileResult(run_id, native_dir, dimos_dir, issues=issues, withheld=withheld)
    result.files += render_native(env, shared, native_dir)
    result.files += render_dimos(env, shared, dimos_dir, usable)

    manifest = {
        "generator": f"bodyboot {__version__}",
        "run_id": run_id,
        "agent_backend": agent_backend,
        "plan_sha256": shared["plan_sha256"],
        "real_motion_enabled": False,
        "hardware_validated": False,
        "capabilities": {name: cap.vendor_symbol for name, cap in usable.items()},
        "withheld": withheld,
    }
    for directory in (native_dir, dimos_dir):
        path = directory / "embodiment_manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        result.files.append(path)
    return result
