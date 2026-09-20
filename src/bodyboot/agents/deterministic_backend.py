"""Deterministic agent backend - reproducible semantic heuristics for CI.

It reads the same snapshot an LLM would and reasons over vocabulary, docstring
units, type shapes and runtime observations. It has no knowledge of any
particular SDK: no vendor class or method name appears in this file, and it
never reads the evaluator's expected answers.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bodyboot.agents.base import AgentBackend, AgentError, AgentResult
from bodyboot.canonical import VECTOR_SIZES, get_capability
from bodyboot.discovery.models import CallableInfo, ClassInfo, FieldInfo, SdkSnapshot
from bodyboot.plan import (
    LOW_CONFIDENCE,
    CapabilityMapping,
    ConnectionPlan,
    EmbodimentPlan,
    FieldSource,
    ParameterMapping,
    dump_plan,
)
from bodyboot.safety.policy import (
    CLOSE_VERBS,
    OPEN_VERBS,
    handle_class_names,
    is_exception_class,
    name_tokens,
)

_WORD = re.compile(r"[a-z]+|\d+")
_UPPER_NAME = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
DEG = math.pi / 180.0
G = 9.80665


def words(text: str | None) -> list[str]:
    return _WORD.findall((text or "").lower())


def first_sentence(text: str | None, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    cut = flat.split(". ")[0]
    return (cut[:limit] + "...") if len(cut) > limit else cut


# --------------------------------------------------------------------------- units


@dataclass(frozen=True)
class UnitRule:
    name_tokens: tuple[str, ...]
    doc_pattern: str
    scale: float
    label: str


UNIT_RULES: dict[str, tuple[UnitRule, ...]] = {
    "time": (
        UnitRule(("us", "usec", "micros"), r"microsecond", 1e-6, "us"),
        UnitRule(("ns", "nsec", "nanos"), r"nanosecond", 1e-9, "ns"),
        UnitRule(("ms", "msec", "millis"), r"millisecond", 1e-3, "ms"),
        UnitRule(("s", "sec", "secs", "seconds"), r"\bseconds?\b", 1.0, "s"),
    ),
    "accel": (
        UnitRule(("g", "gs"), r"standard gravity|units? of g\b|\bin g\b", G, "g"),
        UnitRule(("mps2", "ms2"), r"m/s\^?2|m/s²", 1.0, "m/s^2"),
    ),
    "angular_rate": (
        UnitRule(("dps", "degs"), r"deg(rees)?\s*(/|per)\s*s", DEG, "deg/s"),
        UnitRule(("rps", "rads"), r"rad(ians)?\s*(/|per)\s*s", 1.0, "rad/s"),
    ),
    "angle": (
        UnitRule(("deg", "degs", "degrees"), r"\bdegrees?\b", DEG, "deg"),
        UnitRule(("rad", "rads", "radians"), r"\bradians?\b", 1.0, "rad"),
    ),
    "length": (
        UnitRule(("mm",), r"millimet", 1e-3, "mm"),
        UnitRule(("cm",), r"centimet", 1e-2, "cm"),
        UnitRule(("m", "meters", "metres"), r"\bmet(re|er)s?\b", 1.0, "m"),
    ),
    "speed": (
        UnitRule(("mmps",), r"mm/s", 1e-3, "mm/s"),
        UnitRule(("kph", "kmh"), r"km/h", 1 / 3.6, "km/h"),
        UnitRule(("mps",), r"\bm/s\b", 1.0, "m/s"),
    ),
}


def infer_unit(quantity: str, name: str, doc: str | None) -> tuple[float, str] | None:
    """(scale to canonical SI, vendor unit label) from the identifier, then from the docs."""
    tokens = name_tokens(name)
    rules = UNIT_RULES[quantity]
    for rule in rules:
        if any(token in rule.name_tokens for token in tokens[1:] or tokens):
            return rule.scale, rule.label
    lowered = (doc or "").lower()
    hits = [(m.start(), rule) for rule in rules if (m := re.search(rule.doc_pattern, lowered))]
    if hits:
        rule = min(hits, key=lambda hit: hit[0])[1]
        return rule.scale, rule.label
    return None


# --------------------------------------------------------------------------- profiles


@dataclass(frozen=True)
class FieldProfile:
    name_terms: frozenset[str]
    doc_terms: frozenset[str] = frozenset()
    anti_terms: frozenset[str] = frozenset()
    quantity: str | None = None


def _fp(names: str, docs: str = "", anti: str = "", quantity: str | None = None) -> FieldProfile:
    return FieldProfile(frozenset(names.split()), frozenset(docs.split()), frozenset(anti.split()), quantity)


_TIME = _fp("t ts stamp time timestamp", "timestamp capture", "up uptime", quantity="time")

FIELD_PROFILES: dict[str, dict[str, FieldProfile]] = {
    "camera.rgb": {
        "width": _fp("width cols columns w", "width"),
        "height": _fp("height rows h", "height"),
        "data": _fp("data buf buffer pixels bytes raw payload", "pixel bytes raw"),
        "stamp_s": _TIME,
    },
    "imu": {
        "accel_mps2": _fp("accel acc acceleration", "acceleration force accelerometer",
                          quantity="accel"),
        "gyro_rps": _fp("gyro gyroscope angular omega", "angular gyroscope",
                        quantity="angular_rate"),
        "orientation_xyzw": _fp("quat quaternion orientation attitude", "quaternion orientation"),
        "stamp_s": _TIME,
    },
    "odometry": {
        "position_m": _fp("pos position xyz translation location", "position", quantity="length"),
        "yaw_rad": _fp("yaw heading theta azimuth", "yaw heading", "rate vel speed",
                       quantity="angle"),
        "linear_velocity_mps": _fp("vel velocity speed", "linear velocity",
                                   "turn yaw angular rate omega", quantity="speed"),
        "yaw_rate_rps": _fp("turn yaw angular omega", "yaw rate angular", "heading pos",
                            quantity="angular_rate"),
        "stamp_s": _TIME,
    },
    "connection.health": {
        "alive": _fp("alive up ok connected online healthy reachable", "answered alive connected"),
        "latency_s": _fp("rtt latency ping delay roundtrip", "round trip latency",
                         quantity="time"),
    },
}  # fmt: skip


@dataclass(frozen=True)
class MethodProfile:
    name_terms: dict[str, float]
    doc_terms: dict[str, float]
    anti_terms: dict[str, float] = field(default_factory=dict)


METHOD_PROFILES: dict[str, MethodProfile] = {
    "camera.rgb": MethodProfile(
        {"rgb": 3, "color": 3, "colour": 3, "image": 1.5, "frame": 1, "camera": 1.5, "video": 1,
         "photo": 1, "picture": 1},
        {"rgb": 2, "colour": 2, "color": 2, "camera": 1.5, "image": 1},
        {"mono": 4, "gray": 4, "grey": 4, "depth": 4, "ir": 4, "thermal": 4, "luminance": 3,
         "y": 1},
    ),
    "imu": MethodProfile(
        {"imu": 3, "inertial": 3, "ahrs": 2, "accel": 1, "gyro": 1},
        {"inertial": 2, "imu": 2, "gyroscope": 1, "accelerometer": 1},
        {"battery": 4},
    ),
    "odometry": MethodProfile(
        {"odom": 3, "odometry": 3, "pose": 2, "state": 1.5, "position": 2, "localization": 2,
         "packet": 0.5},
        {"odometry": 2, "position": 1, "pose": 1, "state": 1, "moves": 0.5, "where": 0.5},
        {"imu": 3, "inertial": 3, "battery": 4, "bms": 4},
    ),
    "connection.health": MethodProfile(
        {"heartbeat": 3, "health": 3, "ping": 2, "status": 1.5, "alive": 2, "link": 1.5,
         "keepalive": 3, "watchdog": 1.5},
        {"alive": 1.5, "link": 1, "connection": 1.5, "quality": 1, "supervise": 1, "latency": 1,
         "keep": 0.5},
        {"battery": 4, "firmware": 3},
    ),
    "locomotion.velocity": MethodProfile(
        {"velocity": 3, "vel": 2, "move": 2, "walk": 2, "drive": 2, "joystick": 2, "stick": 1.5,
         "twist": 2, "steer": 1.5, "locomotion": 2, "cmd": 1},
        {"velocity": 2, "setpoint": 1.5, "speed": 1, "walking": 1, "stick": 0.5},
        {"posture": 4, "exposure": 4, "light": 4, "led": 4, "volume": 4},
    ),
    "locomotion.stop": MethodProfile(
        {"stop": 3, "halt": 3, "freeze": 3, "brake": 2.5, "pause": 2, "hold": 1.5, "idle": 1},
        {"halt": 2, "stop": 2, "zeroes": 1.5, "zero": 1.5, "standing": 1.5, "balance": 1,
         "safe": 1, "normal": 0.5},
        {"limp": 5, "damp": 5, "damping": 4, "torque": 4, "collapse": 5, "kill": 5, "power": 4,
         "relax": 4, "emergency": 3, "resort": 3, "fall": 3, "falls": 3, "cut": 2, "sit": 3,
         "lie": 3},
    ),
}  # fmt: skip

AXIS_TERMS: dict[str, frozenset[str]] = {
    "vx": frozenset(["forward", "advance", "longitudinal", "surge", "ahead", "fwd", "vx", "backward"]),
    "vy": frozenset(["strafe", "lateral", "sideways", "sway", "left", "vy", "side"]),
    "yaw_rate": frozenset(["turn", "yaw", "rotate", "rotation", "spin", "angular", "omega", "wz", "clockwise"]),
}


# --------------------------------------------------------------------------- helpers


def annotation_shape(annotation: str | None) -> tuple[str, int]:
    """('vec', n) | ('scalar', 1) | ('bytes', 0) | ('bool', 1) | ('str', 1) | ('unknown', 0)."""
    text = (annotation or "").replace(" ", "").strip("'\"")
    if text in ("bytes", "bytearray", "memoryview"):
        return "bytes", 0
    if text == "bool":
        return "bool", 1
    if text == "str":
        return "str", 1
    if text in ("int", "float"):
        return "scalar", 1
    match = re.fullmatch(r"(?:tuple|Tuple)\[(.+)\]", text)
    if match and "..." not in match.group(1):
        parts = match.group(1).split(",")
        if all(part in ("int", "float") for part in parts):
            return "vec", len(parts)
    return "unknown", 0


def score_terms(tokens: list[str], weights: dict[str, float]) -> float:
    return sum(weights[token] for token in set(tokens) if token in weights)


@dataclass
class Candidate:
    method: CallableInfo
    owner: ClassInfo
    score: float
    detail: dict[str, Any] = field(default_factory=dict)


def _confidence(score: float, runner_up: float, penalties: float, bonus: float) -> float:
    base = 0.55 + 0.42 * (1.0 - math.exp(-max(score, 0.0) / 6.0))
    if runner_up > 0 and runner_up >= 0.85 * score:
        base -= 0.15
    return round(max(0.05, min(0.99, base - penalties + bonus)), 2)


class _Engine:
    def __init__(self, snapshot: SdkSnapshot) -> None:
        self.snapshot = snapshot
        self.handles = handle_class_names(snapshot.all_classes())
        self.constants = {c.name: c for c in snapshot.all_constants()}
        self.warnings: list[str] = []
        self.trace: dict[str, Any] = {}
        self.session = self._find_session()
        self.paths = self._accessor_paths()

    # ---- connection -----------------------------------------------------------

    def _find_session(self) -> ClassInfo:
        probed = self.snapshot.runtime.session_symbol
        if probed and (cls := self.snapshot.find_class(probed)):
            return cls
        best: tuple[float, ClassInfo] | None = None
        for cls in self.snapshot.all_classes():
            if cls.is_dataclass or is_exception_class(cls) or cls.init is None:
                continue
            if cls.init.required_params:
                continue
            verbs = {name_tokens(m.name)[0] for m in cls.public_methods() if name_tokens(m.name)}
            score = 2.0 * bool(verbs & OPEN_VERBS) + 1.0 * bool(verbs & CLOSE_VERBS)
            score += sum(1.0 for m in cls.public_methods() if self._returns_handle(m))
            if best is None or score > best[0]:
                best = (score, cls)
        if best is None or best[0] <= 0:
            raise AgentError("no session-like class (constructible without arguments) was found")
        return best[1]

    def _returns_handle(self, method: CallableInfo) -> bool:
        returned = (method.returns or "").strip("'\" ").rsplit(".", 1)[-1]
        return not method.required_params and returned in self.handles

    def _accessor_paths(self) -> dict[str, list[str]]:
        """Breadth-first accessor chains from the session class to every reachable handle."""
        paths: dict[str, list[str]] = {self.session.qualname: []}
        queue = [self.session]
        while queue:
            cls = queue.pop(0)
            for method in cls.public_methods():
                if not self._returns_handle(method):
                    continue
                target = self.snapshot.find_class(method.returns or "")
                if target is None or target.qualname in paths:
                    continue
                paths[target.qualname] = [*paths[cls.qualname], method.name]
                queue.append(target)
        return paths

    def _lifecycle(self, verbs: frozenset[str]) -> CallableInfo | None:
        for method in self.session.public_methods():
            tokens = name_tokens(method.name)
            if len(tokens) == 1 and tokens[0] in verbs and not method.required_params:
                return method
        return None

    def connection(self) -> ConnectionPlan:
        opener, closer = self._lifecycle(OPEN_VERBS), self._lifecycle(CLOSE_VERBS)
        constructor = self.session.init or opener
        evidence = [
            f"signature: {self.session.qualname}.{constructor.signature()}"
            if constructor
            else f"class: {self.session.qualname}"
        ]
        if self.session.docstring:
            evidence.append(f"docstring: {first_sentence(self.session.docstring)}")
        if self.snapshot.runtime.transport_proof:
            evidence.append(f"runtime observation: {self.snapshot.runtime.transport_proof}")
        return ConnectionPlan(
            session_symbol=self.session.qualname,
            open_method=opener.name if opener else None,
            close_method=closer.name if closer else None,
            evidence=evidence,
        )

    # ---- scoring --------------------------------------------------------------

    def _method_score(self, capability: str, method: CallableInfo, owner: ClassInfo) -> float:
        profile = METHOD_PROFILES[capability]
        name = name_tokens(method.name)
        doc = words(method.docstring)
        owner_words = name_tokens(owner.name) + words(first_sentence(owner.docstring))
        return (
            score_terms(name, profile.name_terms)
            + min(4.0, score_terms(doc, profile.doc_terms))
            + 0.5 * min(3.0, score_terms(owner_words, profile.name_terms | profile.doc_terms))
            - score_terms(name, profile.anti_terms)
            - min(8.0, score_terms(doc, profile.anti_terms))
        )

    def _reachable_methods(self) -> list[tuple[CallableInfo, ClassInfo]]:
        found: list[tuple[CallableInfo, ClassInfo]] = []
        for qualname in self.paths:
            owner = self.snapshot.find_class(qualname)
            if owner is not None:
                found.extend((m, owner) for m in owner.public_methods() if not m.is_property)
        return found

    def _observation(self, method: CallableInfo) -> dict[str, Any] | None:
        for obs in self.snapshot.runtime.observed():
            if obs.symbol == method.qualname and obs.samples and isinstance(obs.samples[0], dict):
                sample: dict[str, Any] = obs.samples[0]
                return sample
        return None

    # ---- sensors --------------------------------------------------------------

    def _match_field(self, capability: str, canonical_field: str, record: ClassInfo) -> tuple[FieldInfo, float] | None:
        profile = FIELD_PROFILES[capability][canonical_field]
        shape = get_capability(capability).field(canonical_field).shape
        best: tuple[FieldInfo, float] | None = None
        for fld in record.fields:
            kind, arity = annotation_shape(fld.annotation)
            wanted = VECTOR_SIZES.get(shape)
            if wanted is not None and (kind != "vec" or arity != wanted):
                continue
            if shape == "bytes" and kind != "bytes":
                continue
            if shape == "bool" and kind != "bool":
                continue
            if shape in ("int", "float") and kind != "scalar":
                continue
            tokens = name_tokens(fld.name)
            doc = words(fld.doc)
            score = 2.0 * len(set(tokens) & profile.name_terms)
            score += min(2.0, float(len(set(doc) & profile.doc_terms)))
            score -= 3.0 * len(set(tokens) & profile.anti_terms)
            if score > 0 and (best is None or score > best[1]):
                best = (fld, score)
        return best

    def _field_source(
        self, capability: str, canonical_field: str, fld: FieldInfo, notes: list[str]
    ) -> tuple[FieldSource, float]:
        """Returns the source and a confidence penalty for undetermined units."""
        profile = FIELD_PROFILES[capability][canonical_field]
        spec = get_capability(capability).field(canonical_field)
        scale, unit, penalty = 1.0, spec.unit if spec.unit == "px" else None, 0.0
        indices: list[int] | None = None
        if profile.quantity:
            inferred = infer_unit(profile.quantity, fld.name, fld.doc)
            if inferred is None:
                penalty = 0.2
                self.warnings.append(
                    f"{capability}.{canonical_field}: unit of vendor field {fld.name!r} could not "
                    "be determined; assumed SI"
                )
            else:
                scale, unit = inferred
        doc = (fld.doc or "").lower()
        tokens = name_tokens(fld.name)
        if spec.shape == "vec4":
            unit = "unit quaternion"
            if "wxyz" in tokens or re.search(r"scalar[ -]first|\(w, ?x, ?y, ?z\)", doc):
                indices = [1, 2, 3, 0]
                notes.append(f"field doc ({fld.name}): scalar-first quaternion -> reordered to xyzw")
            elif not ("xyzw" in tokens or re.search(r"scalar[ -]last|\(x, ?y, ?z, ?w\)", doc)):
                penalty = max(penalty, 0.2)
                self.warnings.append(f"{capability}: quaternion order of {fld.name!r} is undocumented")
        if spec.shape == "vec2":
            group = re.search(r"\(([^)]*)\)", doc)
            order = words(group.group(1)) if group else []
            fwd = [i for i, w in enumerate(order) if w in AXIS_TERMS["vx"]]
            left = [i for i, w in enumerate(order) if w in AXIS_TERMS["vy"]]
            if fwd and left and left[0] < fwd[0]:
                indices = [1, 0]
        if fld.doc:
            notes.append(f"field doc ({fld.name}): {first_sentence(fld.doc, 110)}")
        source = FieldSource(
            canonical_field=canonical_field,
            vendor_paths=[fld.name],
            indices=indices,
            scale=scale,
            vendor_unit=unit,
        )
        return source, penalty

    def _runtime_checks(
        self, capability: str, sources: list[FieldSource], sample: dict[str, Any] | None
    ) -> tuple[float, list[str]]:
        """Cross-check inferred units against what the simulator actually returned."""
        if sample is None:
            return 0.0, []
        by_field = {s.canonical_field: s for s in sources}
        notes: list[str] = []
        bonus = 0.0

        def vendor(name: str) -> Any:
            source = by_field.get(name)
            return sample.get(source.vendor_paths[0]) if source and source.vendor_paths else None

        if capability == "imu":
            accel, source = vendor("accel_mps2"), by_field.get("accel_mps2")
            if isinstance(accel, list) and source:
                norm = math.sqrt(sum(float(a) ** 2 for a in accel)) * abs(source.scale)
                ok = 8.5 <= norm <= 11.0
                notes.append(
                    f"runtime observation: |accel| = {norm:.2f} m/s^2 after scaling "
                    f"({'consistent with gravity' if ok else 'NOT consistent with gravity'})"
                )
                bonus += 0.03 if ok else -0.25
            quat = vendor("orientation_xyzw")
            if isinstance(quat, list) and len(quat) == 4:
                norm = math.sqrt(sum(float(q) ** 2 for q in quat))
                notes.append(f"runtime observation: quaternion norm = {norm:.4f}")
                bonus += 0.02 if abs(norm - 1.0) < 0.01 else -0.2
        if capability == "camera.rgb":
            width, height, data = vendor("width"), vendor("height"), vendor("data")
            if isinstance(data, dict) and isinstance(width, int) and isinstance(height, int):
                ok = data.get("__bytes__") == width * height * 3
                notes.append(
                    f"runtime observation: {data.get('__bytes__')} bytes for {width}x{height} "
                    f"({'= 3 bytes/pixel, packed RGB' if ok else 'NOT 3 bytes per pixel'})"
                )
                bonus += 0.03 if ok else -0.4
        if capability == "connection.health":
            alive = vendor("alive")
            if isinstance(alive, bool):
                notes.append(f"runtime observation: link flag = {alive}")
                bonus += 0.02
        return bonus, notes

    def sensor(self, capability: str) -> CapabilityMapping | None:
        spec = get_capability(capability)
        candidates: list[Candidate] = []
        for method, owner in self._reachable_methods():
            if method.required_params:
                continue
            record = self.snapshot.find_class(method.returns or "")
            if record is None or not record.is_dataclass:
                continue
            matches = {}
            for fld in spec.fields:
                if fld.shape == "str":
                    continue
                match = self._match_field(capability, fld.name, record)
                if match is None:
                    break
                matches[fld.name] = match
            else:
                score = self._method_score(capability, method, owner)
                score += 0.5 * sum(s for _, s in matches.values())
                candidates.append(Candidate(method, owner, score, {"fields": matches}))
        candidates.sort(key=lambda c: c.score, reverse=True)
        self.trace[capability] = [{"symbol": c.method.qualname, "score": round(c.score, 2)} for c in candidates]
        if not candidates or candidates[0].score <= 1.0:
            self.warnings.append(f"{capability}: no vendor symbol matched the canonical contract")
            return None
        best = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        evidence = [
            f"signature: {best.method.qualname.rsplit('.', 2)[-2]}.{best.method.signature()}",
            f"docstring: {first_sentence(best.method.docstring)}",
        ]
        sources: list[FieldSource] = []
        penalty = 0.0
        field_notes: list[str] = []
        for fld in spec.fields:
            if fld.shape == "str":
                sources.append(FieldSource(canonical_field=fld.name, constant=fld.allowed_constants[0]))
                continue
            vendor_field, _ = best.detail["fields"][fld.name]
            source, field_penalty = self._field_source(capability, fld.name, vendor_field, field_notes)
            sources.append(source)
            penalty = max(penalty, field_penalty)
        bonus, runtime_notes = self._runtime_checks(capability, sources, self._observation(best.method))
        evidence += field_notes[:4] + runtime_notes
        if len(candidates) > 1:
            self.warnings.append(
                f"{capability}: rejected look-alike {candidates[1].method.qualname} "
                f"(score {candidates[1].score:.1f} vs {best.score:.1f})"
            )
        return CapabilityMapping(
            canonical_name=capability,
            vendor_symbol=best.method.qualname,
            access_path=self.paths[best.owner.qualname],
            kind=spec.kind,
            returns=best.method.returns,
            fields=sources,
            units={s.canonical_field: f"{s.vendor_unit} x {s.scale:g}" for s in sources if s.vendor_unit},
            frame=spec.frame_hint,
            confidence=_confidence(best.score, runner_up, penalty, bonus),
            risk=spec.risk,
            evidence=evidence,
        )

    # ---- actuators ------------------------------------------------------------

    def _limit(self, doc: str | None) -> tuple[float | None, list[str]]:
        for name in _UPPER_NAME.findall(doc or ""):
            const = self.constants.get(name)
            if const is not None and isinstance(const.value, int | float):
                return float(const.value), [f"constant: {name} = {const.value}"]
        return None, []

    def _map_axes(self, method: CallableInfo) -> tuple[list[ParameterMapping], float, list[str]] | None:
        params = method.required_params
        if len(params) != 3 or any(annotation_shape(p.annotation)[0] != "scalar" for p in params):
            return None
        axes = list(AXIS_TERMS)
        best: tuple[float, tuple[str, ...]] | None = None
        for order in itertools.permutations(axes):
            scores = [
                len(set(words(p.doc) + name_tokens(p.name)) & AXIS_TERMS[axis])
                for p, axis in zip(params, order, strict=True)
            ]
            if min(scores) > 0 and (best is None or sum(scores) > best[0]):
                best = (float(sum(scores)), order)
        if best is None:
            return None
        mappings: list[ParameterMapping] = []
        notes: list[str] = []
        for param, axis in zip(params, best[1], strict=True):
            doc = (param.doc or "").lower()
            quantity = "angular_rate" if axis == "yaw_rate" else "speed"
            inferred = infer_unit(quantity, param.name, param.doc)
            to_si, unit = inferred if inferred else (1.0, None)
            sign = 1.0
            if axis == "yaw_rate" and re.search(r"(?<!counter[- ])(?<!anti[- ])(?<!anti)clockwise", doc):
                sign = -1.0
            if axis == "vy" and re.search(r"positive[^.]*\bright\b", doc) and "left" not in doc:
                sign = -1.0
            if axis == "vx" and re.search(r"positive[^.]*\bbackward", doc):
                sign = -1.0
            limit, limit_notes = self._limit(param.doc)
            mappings.append(
                ParameterMapping(
                    vendor_param=param.name,
                    canonical_source=axis,  # type: ignore[arg-type]
                    scale=sign / to_si,
                    vendor_unit=unit,
                    vendor_min=-limit if limit is not None else None,
                    vendor_max=limit,
                )
            )
            notes.append(f"docstring ({param.name} -> {axis}): {first_sentence(param.doc, 120)}")
            notes += limit_notes
        return mappings, best[0], notes

    def motion(self) -> CapabilityMapping | None:
        capability = "locomotion.velocity"
        spec = get_capability(capability)
        candidates: list[Candidate] = []
        for method, owner in self._reachable_methods():
            mapped = self._map_axes(method)
            if mapped is None:
                continue
            score = self._method_score(capability, method, owner) + mapped[1]
            candidates.append(Candidate(method, owner, score, {"mapped": mapped}))
        candidates.sort(key=lambda c: c.score, reverse=True)
        self.trace[capability] = [{"symbol": c.method.qualname, "score": round(c.score, 2)} for c in candidates]
        if not candidates or candidates[0].score <= 2.0:
            self.warnings.append(f"{capability}: no velocity command found")
            return None
        best = candidates[0]
        mappings, _, notes = best.detail["mapped"]
        penalty = 0.2 if any(m.vendor_unit is None for m in mappings) else 0.0
        vendor_order = [m.canonical_source for m in mappings]
        if vendor_order != ["vx", "vy", "yaw_rate"]:
            self.warnings.append(
                f"{capability}: vendor argument order is {vendor_order}, not canonical (vx, vy, yaw_rate)"
            )
        if any(m.scale < 0 for m in mappings):
            self.warnings.append(f"{capability}: a vendor sign convention is opposite to REP-103")
        self.warnings.append(f"{capability}: mapping rests on documentation only - motion is never probed at runtime")
        return CapabilityMapping(
            canonical_name=capability,
            vendor_symbol=best.method.qualname,
            access_path=self.paths[best.owner.qualname],
            kind=spec.kind,
            parameters=mappings,
            returns=best.method.returns,
            units={m.vendor_param: f"{m.canonical_source} x {m.scale:g} -> {m.vendor_unit}" for m in mappings},
            frame=spec.frame_hint,
            confidence=_confidence(best.score, candidates[1].score if len(candidates) > 1 else 0.0, penalty, 0.0),
            risk=spec.risk,
            evidence=[
                f"signature: {best.owner.name}.{best.method.signature()}",
                f"docstring: {first_sentence(best.method.docstring)}",
                *notes,
            ],
        )

    def stop(self, motion: CapabilityMapping | None) -> CapabilityMapping | None:
        capability = "locomotion.stop"
        spec = get_capability(capability)
        motion_owner = motion.vendor_symbol.rsplit(".", 1)[0] if motion else None
        candidates: list[Candidate] = []
        for method, owner in self._reachable_methods():
            if method.required_params or self._returns_handle(method):
                continue
            score = self._method_score(capability, method, owner)
            if owner.qualname == motion_owner:
                score += 2.0
            candidates.append(Candidate(method, owner, score))
        candidates.sort(key=lambda c: c.score, reverse=True)
        self.trace[capability] = [{"symbol": c.method.qualname, "score": round(c.score, 2)} for c in candidates[:6]]
        if not candidates or candidates[0].score <= 4.0:
            self.warnings.append(f"{capability}: no safe stop command found")
            return None
        best = candidates[0]
        for other in candidates[1:]:
            if other.score < 0 and other.owner.qualname == best.owner.qualname:
                self.warnings.append(
                    f"{capability}: rejected {other.method.qualname} - documented as removing "
                    "torque / letting the robot fall; it is not a stop"
                )
        return CapabilityMapping(
            canonical_name=capability,
            vendor_symbol=best.method.qualname,
            access_path=self.paths[best.owner.qualname],
            kind=spec.kind,
            returns=best.method.returns,
            confidence=_confidence(best.score, max(0.0, candidates[1].score) if len(candidates) > 1 else 0.0, 0.0, 0.0),
            risk=spec.risk,
            evidence=[
                f"signature: {best.owner.name}.{best.method.signature()}",
                f"docstring: {first_sentence(best.method.docstring, 220)}",
            ],
        )

    # ---- plan -----------------------------------------------------------------

    def family(self) -> str:
        corpus = words(" ".join(d.text for d in self.snapshot.documents))
        corpus += words(" ".join(m.docstring or "" for m in self.snapshot.modules))
        legged = sum(corpus.count(w) for w in ("quadruped", "legged", "gait", "foot", "dog", "dogs"))
        return "quadruped (legged, Go2-class)" if legged >= 2 else "mobile robot (unclassified)"

    def build(self) -> EmbodimentPlan:
        capabilities: list[CapabilityMapping] = []
        for name in ("camera.rgb", "imu", "odometry", "connection.health"):
            if (mapped := self.sensor(name)) is not None:
                capabilities.append(mapped)
        motion = self.motion()
        stop = self.stop(motion)
        capabilities += [cap for cap in (motion, stop) if cap is not None]
        order = ["camera.rgb", "imu", "odometry", "locomotion.velocity", "locomotion.stop", "connection.health"]
        capabilities.sort(key=lambda cap: order.index(cap.canonical_name))
        for cap in capabilities:
            if cap.confidence < LOW_CONFIDENCE:
                self.warnings.append(f"{cap.canonical_name}: low confidence ({cap.confidence:.2f})")
        return EmbodimentPlan(
            robot_family=self.family(),
            sdk_package=self.session.qualname.split(".", 1)[0],
            connection=self.connection(),
            capabilities=capabilities,
            warnings=self.warnings,
        )


class DeterministicAgentBackend(AgentBackend):
    name = "deterministic"

    def analyze(self, snapshot: SdkSnapshot, audit_dir: Path) -> AgentResult:
        started = time.perf_counter()
        audit_dir.mkdir(parents=True, exist_ok=True)
        engine = _Engine(snapshot)
        plan = engine.build()
        (audit_dir / "analysis_trace.json").write_text(
            json.dumps({"backend": self.name, "candidates": engine.trace}, indent=2), encoding="utf-8"
        )
        (audit_dir / "raw_response.json").write_text(dump_plan(plan), encoding="utf-8")
        (audit_dir / "prompt.txt").write_text(
            "deterministic backend: no prompt is sent anywhere. Input was sdk_snapshot.json only; "
            "see analysis_trace.json for candidate scores.\n",
            encoding="utf-8",
        )
        return AgentResult(
            plan=plan,
            backend=self.name,
            duration_s=time.perf_counter() - started,
            audit_files=sorted(p.name for p in audit_dir.iterdir()),
        )
