"""Read-only runtime probe (child side). Run as ``python -m bodyboot.discovery.probe_worker``.

Walks live objects breadth-first starting from a session object that is
provably bound to a simulated transport, calling only what the safety policy
classifies as lifecycle / accessor / read. Everything else is recorded as
skipped, with the reason, so the agent can see what was deliberately not touched.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from bodyboot.discovery.models import (
    ClassInfo,
    RuntimeObservation,
    RuntimeProbeReport,
    SdkSnapshot,
)
from bodyboot.safety.policy import (
    ProbeClass,
    classify_probe_call,
    handle_class_names,
    is_exception_class,
    simulated_transport_proof,
)

_MAX_DEPTH = 3
_MAX_OBJECTS = 24
_SAMPLES_PER_READ = 2
_MAX_ITEMS = 16


def summarize(value: Any, depth: int = 0) -> Any:
    """Turn an arbitrary return value into small, JSON-safe evidence."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "..."
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        return {"__bytes__": len(raw), "head_hex": raw[:12].hex()}
    if depth >= 3:
        return {"__type__": type(value).__qualname__}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        summary: dict[str, Any] = {"__type__": type(value).__qualname__}
        for fld in dataclasses.fields(value):
            summary[fld.name] = summarize(getattr(value, fld.name), depth + 1)
        return summary
    if isinstance(value, tuple | list):
        items = [summarize(item, depth + 1) for item in value[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            items.append(f"... {len(value) - _MAX_ITEMS} more")
        return items
    if isinstance(value, dict):
        return {str(k): summarize(v, depth + 1) for k, v in list(value.items())[:_MAX_ITEMS]}
    return {"__type__": type(value).__qualname__}


def _class_info(snapshot: SdkSnapshot, obj: object) -> ClassInfo | None:
    cls = type(obj)
    return snapshot.find_class(f"{cls.__module__}.{cls.__qualname__}")


def _find_session(snapshot: SdkSnapshot) -> tuple[ClassInfo | None, str]:
    constants: dict[str, Any] = {}
    for const in snapshot.all_constants():
        constants.setdefault(const.name, const.value)
    last_reason = "no class can be constructed without arguments"
    for cls in snapshot.all_classes():
        if cls.is_dataclass or is_exception_class(cls) or cls.init is None:
            continue
        proven, reason = simulated_transport_proof(cls, constants)
        if proven:
            return cls, reason
        if not cls.init.required_params:
            last_reason = f"{cls.qualname}: {reason}"
    return None, last_reason


def probe(sdk_path: Path, snapshot: SdkSnapshot) -> RuntimeProbeReport:
    session_cls, proof = _find_session(snapshot)
    if session_cls is None:
        return RuntimeProbeReport(executed=False, reason=f"refused to probe: {proof}")

    sys.path.insert(0, str(sdk_path))
    module_name, class_name = session_cls.qualname.rsplit(".", 1)
    try:
        session = getattr(importlib.import_module(module_name), class_name)()
    except Exception as exc:  # noqa: BLE001 - vendor code may raise anything
        return RuntimeProbeReport(executed=False, reason=f"could not construct {session_cls.qualname}: {exc!r}")

    report = RuntimeProbeReport(
        executed=True,
        reason="read-only probe on simulated transport",
        session_symbol=session_cls.qualname,
        transport_proof=proof,
    )
    sdk_classes = handle_class_names(snapshot.all_classes())
    queue: list[tuple[object, list[str], int]] = [(session, [], 0)]
    seen: set[int] = {id(session)}
    closers: list[Any] = []

    def call(target: Any, is_property: bool) -> tuple[Any, float]:
        started = time.perf_counter()
        result = target if is_property else target()
        return result, (time.perf_counter() - started) * 1000.0

    # Pass 1: bring the link up so reads have something to read.
    info = _class_info(snapshot, session)
    for method in info.public_methods() if info else []:
        decision = classify_probe_call(method, sdk_classes)
        if decision.probe_class is ProbeClass.OPEN:
            obs = RuntimeObservation(
                symbol=method.qualname, policy_class=decision.probe_class, status="ok",
                reason=decision.reason,
            )  # fmt: skip
            try:
                _, obs.duration_ms = call(getattr(session, method.name), False)
            except Exception as exc:  # noqa: BLE001
                obs.status, obs.exception = "error", repr(exc)
            report.observations.append(obs)
        elif decision.probe_class is ProbeClass.CLOSE:
            closers.append((method, decision))

    # Pass 2: breadth-first walk over accessors and reads.
    while queue and len(seen) <= _MAX_OBJECTS:
        obj, path, depth = queue.pop(0)
        info = _class_info(snapshot, obj)
        if info is None:
            continue
        for method in info.public_methods():
            decision = classify_probe_call(method, sdk_classes)
            if decision.probe_class in (ProbeClass.OPEN, ProbeClass.CLOSE):
                continue
            obs = RuntimeObservation(
                symbol=method.qualname,
                access_path=path,
                policy_class=decision.probe_class,
                status="skipped" if not decision.allowed else "ok",
                reason=decision.reason,
            )
            if decision.allowed:
                value: Any = None
                try:
                    repeats = _SAMPLES_PER_READ if decision.probe_class is ProbeClass.READ else 1
                    for _ in range(repeats):
                        bound = getattr(obj, method.name)
                        value, obs.duration_ms = call(bound, method.is_property)
                        obs.return_type = type(value).__qualname__
                        if decision.probe_class is ProbeClass.READ:
                            obs.samples.append(summarize(value))
                    if decision.probe_class is ProbeClass.ACCESSOR and depth < _MAX_DEPTH and id(value) not in seen:
                        seen.add(id(value))
                        queue.append((value, [*path, method.name], depth + 1))
                except Exception as exc:  # noqa: BLE001
                    obs.status, obs.exception = "error", repr(exc)
            report.observations.append(obs)

    # Pass 3: always tear the link down again.
    for method, decision in closers:
        obs = RuntimeObservation(
            symbol=method.qualname, policy_class=decision.probe_class, status="ok",
            reason=decision.reason,
        )  # fmt: skip
        try:
            _, obs.duration_ms = call(getattr(session, method.name), False)
        except Exception as exc:  # noqa: BLE001
            obs.status, obs.exception = "error", repr(exc)
        report.observations.append(obs)
    return report


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: probe_worker <sdk_path> <static_snapshot.json> <report.json>")
        return 2
    sdk_path, snapshot_file, report_file = Path(argv[1]), Path(argv[2]), Path(argv[3])
    snapshot = SdkSnapshot.model_validate(json.loads(snapshot_file.read_text(encoding="utf-8")))
    report = probe(sdk_path, snapshot)
    report_file.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
