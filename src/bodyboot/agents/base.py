"""Agent backend abstraction.

An agent backend turns an ``SdkSnapshot`` into an ``EmbodimentPlan`` and leaves
an audit trail. It is never given the evaluator's expected answers and it never
writes executable code - the deterministic compiler does that.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from bodyboot.discovery.models import SdkSnapshot
from bodyboot.plan import EmbodimentPlan, InvalidPlanError, parse_plan


class AgentError(RuntimeError):
    """Base class for agent stage failures."""


class AgentUnavailableError(AgentError):
    """The requested backend cannot run on this machine."""


class InvalidAgentOutputError(AgentError):
    """The agent answered, but not with a valid embodiment plan."""


@dataclass
class AgentResult:
    plan: EmbodimentPlan
    backend: str
    model: str | None = None
    attempts: int = 1
    duration_s: float = 0.0
    cost_usd: float | None = None
    audit_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class AgentBackend(ABC):
    """Decides semantics. Produces a plan, never code."""

    name: ClassVar[str]

    @abstractmethod
    def analyze(self, snapshot: SdkSnapshot, audit_dir: Path) -> AgentResult:
        """Infer the embodiment plan for ``snapshot``; write audit files into ``audit_dir``."""


def extract_json_object(text: str) -> Any:
    """Pull one JSON object out of an agent reply (tolerates code fences and chatter)."""
    stripped = text.strip()
    if not stripped:
        raise InvalidAgentOutputError("agent returned an empty response")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    position = stripped.find("{")
    while position != -1:
        try:
            value, _ = decoder.raw_decode(stripped[position:])
        except json.JSONDecodeError:
            position = stripped.find("{", position + 1)
            continue
        if isinstance(value, dict):
            return value
        position = stripped.find("{", position + 1)
    raise InvalidAgentOutputError("agent response contains no JSON object")


def plan_from_agent_output(payload: Any) -> EmbodimentPlan:
    """Validate agent output (already-decoded object or raw text) into a plan."""
    data = extract_json_object(payload) if isinstance(payload, str) else payload
    try:
        return parse_plan(data)
    except InvalidPlanError as exc:
        raise InvalidAgentOutputError(f"agent output is not a valid embodiment plan: {exc}") from exc
