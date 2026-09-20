"""Data model of an SDK snapshot - a neutral description of what exists.

Nothing in here assigns meaning. There is no field that says "this is the
camera"; that judgement belongs to the agent stage.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SNAPSHOT_SCHEMA_VERSION = "1.0"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ParamInfo(_Model):
    name: str
    kind: Literal["positional", "keyword_only", "var_positional", "var_keyword"] = "positional"
    annotation: str | None = None
    default: str | None = None
    doc: str | None = None

    @property
    def required(self) -> bool:
        return self.default is None and self.kind in ("positional", "keyword_only")


class CallableInfo(_Model):
    name: str
    qualname: str
    params: list[ParamInfo] = Field(default_factory=list)
    returns: str | None = None
    docstring: str | None = None
    decorators: list[str] = Field(default_factory=list)
    is_property: bool = False
    is_async: bool = False
    lineno: int = 0

    @property
    def is_public(self) -> bool:
        return not self.name.startswith("_")

    @property
    def required_params(self) -> list[ParamInfo]:
        return [p for p in self.params if p.required]

    def signature(self) -> str:
        parts = []
        for p in self.params:
            text = p.name
            if p.annotation:
                text += f": {p.annotation}"
            if p.default is not None:
                text += f" = {p.default}"
            parts.append(text)
        ret = f" -> {self.returns}" if self.returns else ""
        return f"{self.name}({', '.join(parts)}){ret}"


class FieldInfo(_Model):
    name: str
    annotation: str | None = None
    default: str | None = None
    doc: str | None = None


class ClassInfo(_Model):
    name: str
    qualname: str
    bases: list[str] = Field(default_factory=list)
    docstring: str | None = None
    is_dataclass: bool = False
    fields: list[FieldInfo] = Field(default_factory=list)
    init: CallableInfo | None = None
    methods: list[CallableInfo] = Field(default_factory=list)
    lineno: int = 0

    def public_methods(self) -> list[CallableInfo]:
        return [m for m in self.methods if m.is_public]

    def method(self, name: str) -> CallableInfo | None:
        for candidate in self.methods:
            if candidate.name == name:
                return candidate
        return None

    def field(self, name: str) -> FieldInfo | None:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        return None


class ConstantInfo(_Model):
    name: str
    qualname: str
    value: Any = None
    annotation: str | None = None
    doc: str | None = None


class ModuleInfo(_Model):
    name: str
    path: str
    docstring: str | None = None
    classes: list[ClassInfo] = Field(default_factory=list)
    functions: list[CallableInfo] = Field(default_factory=list)
    constants: list[ConstantInfo] = Field(default_factory=list)


class DocumentInfo(_Model):
    path: str
    text: str
    truncated: bool = False


class RuntimeObservation(_Model):
    symbol: str
    access_path: list[str] = Field(default_factory=list)
    policy_class: str
    status: Literal["ok", "error", "skipped"]
    reason: str = ""
    return_type: str | None = None
    samples: list[Any] = Field(default_factory=list)
    exception: str | None = None
    duration_ms: float | None = None


class RuntimeProbeReport(_Model):
    executed: bool = False
    reason: str = ""
    session_symbol: str | None = None
    transport_proof: str | None = None
    observations: list[RuntimeObservation] = Field(default_factory=list)

    def observed(self) -> list[RuntimeObservation]:
        return [o for o in self.observations if o.status == "ok"]


class SnapshotStats(_Model):
    modules: int = 0
    classes: int = 0
    public_symbols: int = 0
    constants: int = 0
    runtime_calls: int = 0
    runtime_skipped: int = 0


class SdkSnapshot(_Model):
    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    sdk_name: str
    packages: list[str] = Field(default_factory=list)
    modules: list[ModuleInfo] = Field(default_factory=list)
    documents: list[DocumentInfo] = Field(default_factory=list)
    runtime: RuntimeProbeReport = Field(default_factory=RuntimeProbeReport)
    stats: SnapshotStats = Field(default_factory=SnapshotStats)

    def all_classes(self) -> list[ClassInfo]:
        return [cls for module in self.modules for cls in module.classes]

    def all_constants(self) -> list[ConstantInfo]:
        return [const for module in self.modules for const in module.constants]

    def find_class(self, name: str) -> ClassInfo | None:
        """Look a class up by qualified name, falling back to its bare name."""
        for cls in self.all_classes():
            if cls.qualname == name:
                return cls
        bare = name.rsplit(".", 1)[-1].strip("'\" ")
        matches = [cls for cls in self.all_classes() if cls.name == bare]
        return matches[0] if len(matches) == 1 else None

    def find_callable(self, qualname: str) -> CallableInfo | None:
        for module in self.modules:
            for function in module.functions:
                if function.qualname == qualname:
                    return function
            for cls in module.classes:
                for method in cls.methods:
                    if method.qualname == qualname:
                        return method
        return None

    def owner_of(self, qualname: str) -> ClassInfo | None:
        for cls in self.all_classes():
            if any(m.qualname == qualname for m in cls.methods):
                return cls
        return None

    def recompute_stats(self) -> None:
        classes = self.all_classes()
        public = sum(1 for c in classes if not c.name.startswith("_"))
        public += sum(len(c.public_methods()) for c in classes)
        public += sum(1 for m in self.modules for f in m.functions if f.is_public)
        self.stats = SnapshotStats(
            modules=len(self.modules),
            classes=len(classes),
            public_symbols=public,
            constants=len(self.all_constants()),
            runtime_calls=len(self.runtime.observed()),
            runtime_skipped=sum(1 for o in self.runtime.observations if o.status == "skipped"),
        )
