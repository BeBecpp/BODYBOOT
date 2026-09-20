"""Child process that exercises a generated native adapter against the vendor SDK.

Run as ``python -m bodyboot.verification.native_runner <job.json> <facts.json>``.

It only *collects facts*; judging them happens in the parent. Every public
method of every vendor class is wrapped in a spy first, so the parent can prove
which vendor calls each adapter method did - and did not - make.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

SAMPLES = 3


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, bytes | bytearray):
        raw = bytes(value)
        return {"__bytes__": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(value, tuple | list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return repr(value)


def _attr(obj: Any, dotted: str) -> Any:
    for segment in dotted.split("."):
        obj = getattr(obj, segment)
    return obj


def _walk(obj: Any, access_path: list[str]) -> Any:
    for name in access_path:
        obj = getattr(obj, name)
        if callable(obj):
            obj = obj()
    return obj


def reference_value(record: Any, rule: dict[str, Any]) -> Any:
    """Evaluator-side conversion of a vendor record field (independent of the plan)."""
    if "constant" in rule:
        return rule["constant"]
    value = _attr(record, rule["path"])
    scale = rule.get("scale")
    if "indices" in rule:
        value = [value[i] for i in rule["indices"]]
    if isinstance(value, tuple | list):
        return [float(v) * scale if scale is not None else v for v in value]
    if isinstance(value, bytes | bytearray):
        return jsonable(value)
    return float(value) * scale if scale is not None else value


def collect_reference(expected: dict[str, Any]) -> dict[str, Any]:
    """Read ground-truth values straight from the vendor SDK via the evaluator's symbols."""
    session_info = expected["session"]
    module_name, class_name = session_info["symbol"].rsplit(".", 1)
    session = getattr(importlib.import_module(module_name), class_name)()
    if session_info.get("open"):
        getattr(session, session_info["open"])()
    reference: dict[str, Any] = {}
    for name, spec in expected["capabilities"].items():
        rules = spec.get("reference")
        if not rules:
            continue
        owner = _walk(session, spec.get("access_path", []))
        method = spec["vendor_symbols"][0].rsplit(".", 1)[-1]
        samples = []
        for _ in range(SAMPLES):
            record = getattr(owner, method)()
            samples.append({fld: reference_value(record, rule) for fld, rule in rules.items()})
        reference[name] = samples
    if session_info.get("close"):
        getattr(session, session_info["close"])()
    return reference


def install_spies(class_symbols: list[str], log: list[str]) -> int:
    """Wrap every public method of the listed vendor classes; returns how many were wrapped."""
    wrapped = 0
    for symbol in class_symbols:
        module_name, class_name = symbol.rsplit(".", 1)
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:  # noqa: BLE001
            continue
        for name, member in list(vars(cls).items()):
            if name.startswith("_") or not inspect.isfunction(member):
                continue

            def make(original: Any, qualname: str) -> Any:
                def spy(*args: Any, **kwargs: Any) -> Any:
                    log.append(qualname)
                    return original(*args, **kwargs)

                return spy

            setattr(cls, name, make(member, f"{symbol}.{name}"))
            wrapped += 1
    return wrapped


def attempt(facts: dict[str, Any], key: str, action: Any) -> Any:
    """Run ``action``; record the outcome (or the exception type/message) under ``key``."""
    try:
        value = action()
        facts[key] = {"ok": True, "value": jsonable(value)}
        return value
    except Exception as exc:  # noqa: BLE001
        facts[key] = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}
        return None


def run(job: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    sys.path.insert(0, job["sdk_path"])
    sys.path.insert(0, job["native_dir"])
    os.environ["BODYBOOT_ENABLE_REAL_MOTION"] = "1"  # must have no effect whatsoever
    os.environ["REAL_MOTION_ENABLED"] = "1"

    expected = job.get("expected")
    if expected:
        attempt(facts, "reference", lambda: collect_reference(expected))

    attempt(facts, "import", lambda: importlib.import_module(job["package"]).__name__)
    if not facts["import"]["ok"]:
        return facts
    package = importlib.import_module(job["package"])
    adapter_cls = package.GeneratedRobotAdapter
    facts["real_motion_flag"] = bool(package.REAL_MOTION_ENABLED)
    facts["api"] = {
        name: callable(getattr(adapter_cls, name, None))
        for name in ("connect", "health", "camera_rgb", "imu", "odometry", "velocity", "stop", "close")
    }
    facts["velocity_params"] = [p for p in inspect.signature(adapter_cls.velocity).parameters if p != "self"]

    log: list[str] = []
    facts["spies_installed"] = install_spies(job["vendor_classes"], log)

    adapter = adapter_cls()
    attempt(facts, "use_before_connect", adapter.imu)
    attempt(facts, "connect", adapter.connect)

    for key, method in (("connection.health", "health"), ("camera.rgb", "camera_rgb"), ("imu", "imu"),
                        ("odometry", "odometry")):  # fmt: skip
        samples, calls = [], []
        for index in range(SAMPLES):
            del log[:]
            attempt(facts, f"_{key}_{index}", getattr(adapter, method))
            samples.append(facts.pop(f"_{key}_{index}"))
            calls.append(list(log))
        facts[f"sensor:{key}"] = {"samples": samples, "vendor_calls": calls}

    motion = []
    for vector in job["velocity_vectors"]:
        del log[:]
        outcome: dict[str, Any] = {"canonical": vector}
        attempt(outcome, "result", lambda v=vector: adapter.velocity(v["vx"], v["vy"], v["yaw_rate"]))
        outcome["vendor_calls"] = list(log)
        motion.append(outcome)
    facts["motion"] = motion

    for label, bad in (("nan", float("nan")), ("inf", float("inf"))):
        del log[:]
        attempt(facts, f"velocity_{label}", lambda b=bad: adapter.velocity(b, 0.0, 0.0))
        facts[f"velocity_{label}"]["vendor_calls"] = list(log)

    del log[:]
    attempt(facts, "stop", adapter.stop)
    facts["stop"]["vendor_calls"] = list(log)
    del log[:]
    attempt(facts, "velocity_after_stop", lambda: adapter.velocity(0.1, 0.0, 0.0))
    facts["velocity_after_stop"]["vendor_calls"] = list(log)

    attempt(facts, "enable_real_motion", adapter.enable_real_motion)
    attempt(facts, "allow_real_motion_ctor", lambda: adapter_cls(allow_real_motion=True))
    facts["command_log"] = jsonable(adapter.command_log)

    attempt(facts, "close", adapter.close)
    attempt(facts, "close_again", adapter.close)
    attempt(facts, "use_after_close", adapter.imu)
    return facts


def main(argv: list[str]) -> int:
    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        facts = run(job)
    except Exception:  # noqa: BLE001
        facts = {"fatal": traceback.format_exc()[-2000:]}
    Path(argv[2]).write_text(json.dumps(facts, indent=1, allow_nan=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
