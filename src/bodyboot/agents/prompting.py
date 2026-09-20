"""Prompt construction for LLM agent backends.

The prompt contains exactly three things: BODYBOOT's mapping instructions (incl.
the canonical contract), the SDK snapshot, and the output schema. The
evaluator's expected answers must never appear in it - ``assert_no_evaluator_leak``
enforces that on every prompt before it leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

from bodyboot.canonical import describe_contract
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.plan import plan_json_schema

FORBIDDEN_PROMPT_MARKERS: tuple[str, ...] = (
    "expected_capabilities",
    "BODYBOOT-EVALUATOR",
    "fixtures/evaluator",
    "fixtures\\evaluator",
)

SYSTEM_PROMPT = (
    "You are the semantic-analysis stage of BODYBOOT, an embodiment compiler for robots. "
    "You read a machine-generated description of an unfamiliar vendor robot SDK and decide "
    "which vendor symbols implement BODYBOOT's canonical capabilities, including units, "
    "argument order, sign conventions and coordinate frames. You never write code. You "
    "answer with a single JSON object that conforms to the given schema and nothing else."
)

_INSTRUCTIONS = """\
# Task

Below is `sdk_snapshot.json`: a neutral description of an unfamiliar robot vendor SDK
(modules, classes, signatures, type annotations, docstrings, constants, vendor documents,
and observations from a read-only runtime probe on the vendor's simulator transport).
Nobody has written an adapter for this SDK. Infer how the SDK embodies the canonical
contract and answer with an **embodiment plan** as JSON.

A deterministic compiler will turn your plan into adapter code, so every number and name
you give is used literally. Be exact.

# Canonical contract (the target side)

{contract}

Canonical conventions (REP-103): x forward, y left, z up; angles and angular rates are
counter-clockwise positive seen from above; SI units (m, m/s, rad, rad/s, m/s^2, s);
quaternions are scalar-LAST (x, y, z, w).

# Rules

1. Use only symbols that exist in the snapshot. Never invent a symbol.
   `vendor_symbol` is the fully qualified callable: `package.module.Class.method`.
2. `connection.session_symbol` is the class to instantiate (`package.module.Class`).
   Leave `constructor_kwargs` empty unless a value is truly required; vendor defaults are
   preferred. Give `open_method` / `close_method` if the SDK has them.
3. `access_path` lists the zero-argument accessors to call on the session object, in order,
   to reach the object that owns the vendor callable. Use `[]` if the session owns it.
4. Sensors / status: give exactly one `fields` entry per canonical field.
   `canonical_value = vendor_value * scale`. `vendor_paths` are attribute paths on the
   object the vendor call returns: ONE path for a scalar or for a vendor sequence holding
   the whole vector; N paths if the vendor stores vector components separately. Use
   `indices` to pick/reorder elements of a vendor sequence into canonical order. For the
   image `encoding`, set `constant` instead of a path. Always fill `vendor_unit`.
5. `locomotion.velocity`: give one `parameters` entry per VENDOR parameter, stating which
   canonical argument (`vx`, `vy`, `yaw_rate`) feeds it. `vendor_value = canonical_value *
   scale`; the scale carries both the unit conversion and the sign convention. Vendor
   argument order frequently differs from canonical order - read the parameter docs, do not
   assume. Put documented vendor limits in `vendor_min` / `vendor_max` (vendor units).
6. `locomotion.stop` must be the command that halts walking while the robot stays standing
   and balanced. A command that removes joint torque or lets the robot fall is NOT a stop.
7. `risk` must be `read_only` for sensors/status, `motion` for the velocity command, `stop`
   for the stop command.
8. Every mapping needs `evidence`: short items, each prefixed with its source
   (`docstring:`, `signature:`, `runtime observation:`, `constant:`, `readme:`), quoting the
   snapshot. Prefer several independent sources. Runtime observations are good for checking
   units (e.g. the magnitude of an accelerometer reading at rest).
9. `confidence` is a calibrated probability in [0, 1] that the mapping, including units and
   signs, is correct.
10. If a capability truly cannot be mapped, omit it and explain in `warnings`. Also use
    `warnings` for anything a human integrator should double-check, e.g. look-alike symbols
    you deliberately rejected.

# Output

Return ONLY one JSON object conforming to this JSON schema. No markdown, no commentary.

{schema}

# sdk_snapshot.json

{snapshot}
"""


def _prune(value: Any) -> Any:
    """Drop empty/None members and line numbers to keep the prompt small and neutral."""
    if isinstance(value, dict):
        pruned = {
            # Runtime samples are evidence: keep them verbatim (zeros and False matter there).
            k: (v if k == "samples" else _prune(v))
            for k, v in value.items()
            if k not in ("lineno", "stats")
        }
        return {k: v for k, v in pruned.items() if not _is_empty(v)}
    if isinstance(value, list):
        return [_prune(item) for item in value]
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value is False or (isinstance(value, str | list | dict) and not value)


def snapshot_for_agent(snapshot: SdkSnapshot) -> dict[str, Any]:
    pruned: dict[str, Any] = _prune(snapshot.model_dump(mode="json"))
    return pruned


def assert_no_evaluator_leak(text: str) -> None:
    """Hard guard: evaluator material must never reach an agent."""
    lowered = text.lower()
    for marker in FORBIDDEN_PROMPT_MARKERS:
        if marker.lower() in lowered:
            raise AssertionError(f"refusing to send prompt: it contains evaluator material ({marker!r})")


def build_prompt(snapshot: SdkSnapshot) -> str:
    """The full user prompt for an LLM agent backend."""
    prompt = _INSTRUCTIONS.format(
        contract=describe_contract(),
        schema=json.dumps(plan_json_schema(), indent=1),
        snapshot=json.dumps(snapshot_for_agent(snapshot), indent=1),
    )
    assert_no_evaluator_leak(prompt)
    return prompt


def build_repair_prompt(original_prompt: str, previous_response: str, error: str) -> str:
    """Re-ask after a *structural* failure. Carries only the schema error - no answers."""
    prompt = (
        f"{original_prompt}\n\n# Your previous answer was rejected\n\n"
        f"It was not a valid embodiment plan. Validation error:\n\n{error[:3000]}\n\n"
        f"Previous answer (truncated):\n\n{previous_response[:3000]}\n\n"
        "Return the corrected, complete JSON object only."
    )
    assert_no_evaluator_leak(prompt)
    return prompt
