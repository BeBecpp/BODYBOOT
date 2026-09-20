"""The embodiment plan - the ONLY artifact an agent is allowed to produce.

The agent decides semantics (which vendor symbol is the IMU, which argument is
the yaw rate, what unit it is in). It never writes code. Everything that ends
up inside generated source is validated here as a plain identifier or number,
so an agent response cannot inject executable text.
"""

from __future__ import annotations

import json
import keyword
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from bodyboot.canonical import (
    MOTION_PARAMS,
    NUMERIC_SHAPES,
    REQUIRED_CAPABILITIES,
    VECTOR_SIZES,
    get_capability,
    is_canonical,
)

PLAN_SCHEMA_VERSION = "1.0"
LOW_CONFIDENCE = 0.6
"""Below this confidence a mapping is reported as a warning."""

_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _check_identifier(value: str) -> str:
    if not _SEGMENT.match(value) or keyword.iskeyword(value):
        raise ValueError(
            f"{value!r} is not a plain public identifier (letters, digits, underscore; "
            "must not start with an underscore or be a Python keyword)"
        )
    return value


def _check_dotted(value: str) -> str:
    if not value:
        raise ValueError("dotted path must not be empty")
    for segment in value.split("."):
        _check_identifier(segment)
    return value


def _check_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("number must be finite")
    return value


Identifier = Annotated[str, AfterValidator(_check_identifier)]
DottedPath = Annotated[str, AfterValidator(_check_dotted)]
Finite = Annotated[float, AfterValidator(_check_finite)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class FieldSource(_Model):
    """How one canonical output field is obtained from the vendor return value."""

    canonical_field: str = Field(description="Name of the canonical field being filled.")
    vendor_paths: list[DottedPath] = Field(
        default_factory=list,
        description=(
            "Attribute path(s) on the object returned by the vendor call. One path for a "
            "scalar or for a vendor sequence that already holds the whole vector; N paths "
            "when the vendor stores the N vector components as separate attributes."
        ),
    )
    indices: list[int] | None = Field(
        default=None,
        description=(
            "Only with a single vendor sequence: which vendor elements to take, in canonical "
            "order. Example: vendor quaternion (w,x,y,z) -> canonical (x,y,z,w) is [1,2,3,0]."
        ),
    )
    scale: Finite = Field(
        default=1.0,
        description="canonical_value = vendor_value * scale (unit conversion and sign).",
    )
    constant: str | None = Field(
        default=None,
        description="Fixed canonical value for string fields such as the image encoding.",
    )
    vendor_unit: str | None = Field(default=None, description="Unit of the vendor value, e.g. 'deg/s', 'g', 'us'.")


class ParameterMapping(_Model):
    """How one VENDOR argument of a command is computed from canonical arguments."""

    vendor_param: Identifier = Field(description="Name of the vendor function parameter.")
    canonical_source: Literal["vx", "vy", "yaw_rate"] | None = Field(
        default=None, description="Canonical argument feeding this vendor parameter."
    )
    scale: Finite = Field(
        default=1.0,
        description=(
            "vendor_value = canonical_value * scale. Encodes unit conversion AND sign "
            "convention (e.g. rad/s CCW+ -> deg/s CW+ is -57.29577951308232)."
        ),
    )
    constant: Finite | None = Field(default=None, description="Fixed vendor value when no canonical argument applies.")
    vendor_unit: str | None = Field(default=None, description="Unit of the vendor parameter.")
    vendor_min: Finite | None = Field(default=None, description="Documented vendor lower limit.")
    vendor_max: Finite | None = Field(default=None, description="Documented vendor upper limit.")


class CapabilityMapping(_Model):
    """One canonical capability mapped onto one vendor callable."""

    canonical_name: str = Field(description="Canonical capability name, e.g. 'camera.rgb'.")
    vendor_symbol: DottedPath = Field(description="Fully qualified vendor callable: package.module.Class.method")
    access_path: list[Identifier] = Field(
        default_factory=list,
        description=(
            "Zero-argument accessors (methods or attributes) to walk from the session object "
            "to the object that owns the vendor callable. Empty when the session owns it."
        ),
    )
    kind: Literal["sensor", "actuator", "status"]
    parameters: list[ParameterMapping] = Field(default_factory=list)
    returns: str | None = Field(default=None, description="Vendor return type name.")
    fields: list[FieldSource] = Field(default_factory=list, description="One entry per canonical output field.")
    units: dict[str, str] = Field(
        default_factory=dict, description="Free-form summary of the unit conversions applied."
    )
    frame: str | None = Field(default=None, description="Coordinate frame of the data/command.")
    confidence: float = Field(ge=0.0, le=1.0)
    risk: Literal["read_only", "motion", "stop"]
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "Why this mapping is believed. Prefix each item with its source: 'docstring: ...', "
            "'signature: ...', 'runtime observation: ...', 'constant: ...', 'readme: ...'."
        ),
    )

    @property
    def method_name(self) -> str:
        return self.vendor_symbol.rsplit(".", 1)[-1]

    def field_source(self, canonical_field: str) -> FieldSource | None:
        for source in self.fields:
            if source.canonical_field == canonical_field:
                return source
        return None


class ConnectionPlan(_Model):
    """How to obtain a live session object from the vendor SDK."""

    session_symbol: DottedPath = Field(description="Fully qualified class to instantiate: package.module.Class")
    constructor_kwargs: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Keyword arguments for the constructor. Leave empty to use vendor defaults.",
    )
    open_method: Identifier | None = Field(
        default=None, description="Zero-argument method that brings the link up, if any."
    )
    close_method: Identifier | None = Field(
        default=None, description="Zero-argument method that tears the link down, if any."
    )
    evidence: list[str] = Field(min_length=1)

    @property
    def module(self) -> str:
        return self.session_symbol.rsplit(".", 1)[0]

    @property
    def class_name(self) -> str:
        return self.session_symbol.rsplit(".", 1)[-1]


class EmbodimentPlan(_Model):
    """Agent-inferred description of how a vendor SDK embodies the canonical contract."""

    schema_version: Literal["1.0"] = "1.0"
    robot_family: str = Field(description="Short description, e.g. 'quadruped (Go2-like)'.")
    sdk_package: Identifier = Field(description="Top-level importable vendor package.")
    connection: ConnectionPlan
    capabilities: list[CapabilityMapping]
    warnings: list[str] = Field(default_factory=list)

    def get(self, canonical_name: str) -> CapabilityMapping | None:
        for capability in self.capabilities:
            if capability.canonical_name == canonical_name:
                return capability
        return None

    def names(self) -> list[str]:
        return [capability.canonical_name for capability in self.capabilities]


class InvalidPlanError(ValueError):
    """The agent output is not a structurally valid embodiment plan."""


def plan_json_schema() -> dict[str, Any]:
    """JSON schema handed to the agent."""
    return EmbodimentPlan.model_json_schema()


def parse_plan(data: Any) -> EmbodimentPlan:
    """Validate arbitrary decoded JSON into a plan (raises InvalidPlanError)."""
    if not isinstance(data, dict):
        raise InvalidPlanError(f"plan must be a JSON object, got {type(data).__name__}")
    try:
        return EmbodimentPlan.model_validate(data)
    except ValidationError as exc:
        raise InvalidPlanError(str(exc)) from exc


def load_plan(path: Path) -> EmbodimentPlan:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidPlanError(f"{path}: not valid JSON ({exc})") from exc
    return parse_plan(data)


def dump_plan(plan: EmbodimentPlan) -> str:
    return json.dumps(plan.model_dump(mode="json"), indent=2) + "\n"


@dataclass(frozen=True)
class PlanIssue:
    """A semantic problem found in a structurally valid plan."""

    severity: Literal["error", "warning"]
    capability: str | None
    message: str

    def render(self) -> str:
        scope = f"[{self.capability}] " if self.capability else ""
        return f"{self.severity.upper()}: {scope}{self.message}"


def _check_field_sources(cap: CapabilityMapping, issues: list[PlanIssue]) -> None:
    spec = get_capability(cap.canonical_name)
    name = cap.canonical_name
    seen: set[str] = set()
    for mapped in cap.fields:
        if mapped.canonical_field in seen:
            issues.append(PlanIssue("error", name, f"field {mapped.canonical_field} mapped twice"))
        seen.add(mapped.canonical_field)
    for fld in spec.fields:
        source = cap.field_source(fld.name)
        if source is None:
            issues.append(PlanIssue("error", name, f"canonical field {fld.name} is not mapped"))
            continue
        if source.constant is not None:
            if fld.shape != "str":
                issues.append(PlanIssue("error", name, f"{fld.name}: constants are only valid for str fields"))
            elif fld.allowed_constants and source.constant not in fld.allowed_constants:
                issues.append(
                    PlanIssue(
                        "error",
                        name,
                        f"{fld.name}: constant {source.constant!r} not in {list(fld.allowed_constants)}",
                    )
                )
            continue
        if not source.vendor_paths:
            issues.append(PlanIssue("error", name, f"{fld.name}: no vendor path and no constant"))
            continue
        size = VECTOR_SIZES.get(fld.shape)
        if size is None:
            if len(source.vendor_paths) != 1:
                issues.append(PlanIssue("error", name, f"{fld.name}: scalar needs exactly 1 path"))
            if source.indices is not None and len(source.indices) != 1:
                issues.append(PlanIssue("error", name, f"{fld.name}: scalar takes at most 1 index"))
        else:
            if len(source.vendor_paths) not in (1, size):
                issues.append(PlanIssue("error", name, f"{fld.name}: needs 1 sequence path or {size} paths"))
            if source.indices is not None:
                if len(source.vendor_paths) != 1:
                    issues.append(PlanIssue("error", name, f"{fld.name}: indices need a single sequence path"))
                if len(source.indices) != size:
                    issues.append(PlanIssue("error", name, f"{fld.name}: indices must have {size} entries"))
        if source.indices is not None and any(i < 0 or i > 63 for i in source.indices):
            issues.append(PlanIssue("error", name, f"{fld.name}: index out of range"))
        if fld.shape in NUMERIC_SHAPES:
            if source.scale == 0.0:
                issues.append(PlanIssue("error", name, f"{fld.name}: scale must not be zero"))
            if fld.unit and fld.unit != "px" and not source.vendor_unit:
                issues.append(PlanIssue("warning", name, f"{fld.name}: vendor unit is not documented"))


def _check_parameters(cap: CapabilityMapping, issues: list[PlanIssue]) -> None:
    spec = get_capability(cap.canonical_name)
    name = cap.canonical_name
    vendor_names = [p.vendor_param for p in cap.parameters]
    if len(set(vendor_names)) != len(vendor_names):
        issues.append(PlanIssue("error", name, "a vendor parameter is mapped more than once"))
    if spec.risk == "motion":
        sources: list[str] = [p.canonical_source for p in cap.parameters if p.canonical_source]
        for wanted in MOTION_PARAMS:
            count = sources.count(wanted)
            if count != 1:
                issues.append(
                    PlanIssue(
                        "error",
                        name,
                        f"canonical argument {wanted} must feed exactly 1 vendor parameter (found {count})",
                    )
                )
        for param in cap.parameters:
            if param.canonical_source is None and param.constant is None:
                issues.append(
                    PlanIssue(
                        "error",
                        name,
                        f"vendor parameter {param.vendor_param} has neither a canonical source nor a constant",
                    )
                )
            if param.canonical_source is not None:
                if param.scale == 0.0:
                    issues.append(PlanIssue("error", name, f"{param.vendor_param}: scale must not be zero"))
                if not param.vendor_unit:
                    issues.append(PlanIssue("warning", name, f"{param.vendor_param}: vendor unit missing"))
            if param.vendor_min is not None and param.vendor_max is not None and param.vendor_min > param.vendor_max:
                issues.append(PlanIssue("error", name, f"{param.vendor_param}: min exceeds max"))
    elif any(p.canonical_source is not None for p in cap.parameters):
        issues.append(PlanIssue("error", name, "only the motion capability may consume canonical arguments"))


def validate_plan(plan: EmbodimentPlan) -> list[PlanIssue]:
    """Semantic validation against the canonical contract. Never raises."""
    issues: list[PlanIssue] = []
    seen: set[str] = set()
    for cap in plan.capabilities:
        name = cap.canonical_name
        if not is_canonical(name):
            issues.append(PlanIssue("warning", name, "unknown canonical capability - ignored"))
            continue
        if name in seen:
            issues.append(PlanIssue("error", name, "capability mapped more than once"))
            continue
        seen.add(name)
        spec = get_capability(name)
        if cap.risk != spec.risk:
            issues.append(PlanIssue("error", name, f"risk must be {spec.risk!r} for this capability, got {cap.risk!r}"))
        if cap.kind != spec.kind:
            issues.append(PlanIssue("warning", name, f"kind should be {spec.kind!r}"))
        if cap.confidence < LOW_CONFIDENCE:
            issues.append(PlanIssue("warning", name, f"low confidence mapping ({cap.confidence:.2f})"))
        _check_field_sources(cap, issues)
        _check_parameters(cap, issues)
    for required in REQUIRED_CAPABILITIES:
        if required not in seen:
            issues.append(PlanIssue("error", required, "required capability is missing"))
    return issues


def errors_of(issues: list[PlanIssue]) -> list[PlanIssue]:
    return [issue for issue in issues if issue.severity == "error"]
