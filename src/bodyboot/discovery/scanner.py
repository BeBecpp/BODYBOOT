"""Static SDK scanner.

Pure ``ast`` - the vendor code is never imported or executed here. The scanner
records modules, classes, signatures, annotations, docstrings and literal
constants. It performs no semantic mapping of any kind.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from bodyboot.discovery.models import (
    CallableInfo,
    ClassInfo,
    ConstantInfo,
    DocumentInfo,
    FieldInfo,
    ModuleInfo,
    ParamInfo,
    SdkSnapshot,
)

_SKIP_DIRS = {"__pycache__", "tests", "test", "build", "dist", "node_modules"}
_DOC_SUFFIXES = {".md", ".rst", ".txt"}
_MAX_DOC_CHARS = 6000
_SECTION = re.compile(r"^(?P<indent>\s*)(?P<title>[A-Z][A-Za-z ]+):\s*$")
_ENTRY = re.compile(r"^(?P<indent>\s+)\*{0,2}(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?:\s*(?P<text>.*)$")


class ScanError(RuntimeError):
    """The path does not look like a scannable Python SDK."""


def parse_doc_section(docstring: str | None, titles: tuple[str, ...]) -> dict[str, str]:
    """Extract ``name: description`` entries from a Google-style docstring section."""
    if not docstring:
        return {}
    entries: dict[str, str] = {}
    lines = docstring.expandtabs(4).splitlines()
    in_section = False
    section_indent = 0
    entry_indent: int | None = None
    current: str | None = None
    for line in lines:
        header = _SECTION.match(line)
        if header:
            in_section = header.group("title").strip() in titles
            section_indent = len(header.group("indent"))
            entry_indent = None
            current = None
            continue
        if not in_section:
            continue
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= section_indent:
            in_section = False
            current = None
            continue
        entry = _ENTRY.match(line)
        if entry and (entry_indent is None or indent == entry_indent):
            entry_indent = indent
            current = entry.group("name")
            entries[current] = entry.group("text").strip()
        elif current is not None:
            entries[current] = f"{entries[current]} {line.strip()}".strip()
    return entries


def _unparse(node: ast.AST | None) -> str | None:
    return None if node is None else ast.unparse(node)


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef, docs: dict[str, str]) -> list[ParamInfo]:
    args = node.args
    params: list[ParamInfo] = []
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults.extend(args.defaults)
    for arg, default in zip(positional, defaults, strict=True):
        if arg.arg in ("self", "cls") and not params:
            continue
        params.append(
            ParamInfo(
                name=arg.arg,
                annotation=_unparse(arg.annotation),
                default=_unparse(default),
                doc=docs.get(arg.arg),
            )
        )
    if args.vararg:
        params.append(
            ParamInfo(
                name=args.vararg.arg,
                kind="var_positional",
                annotation=_unparse(args.vararg.annotation),
            )
        )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        params.append(
            ParamInfo(
                name=arg.arg,
                kind="keyword_only",
                annotation=_unparse(arg.annotation),
                default=_unparse(default),
                doc=docs.get(arg.arg),
            )
        )
    if args.kwarg:
        params.append(ParamInfo(name=args.kwarg.arg, kind="var_keyword", annotation=_unparse(args.kwarg.annotation)))
    return params


def _callable(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str) -> CallableInfo:
    docstring = ast.get_docstring(node)
    decorators = [ast.unparse(d) for d in node.decorator_list]
    return CallableInfo(
        name=node.name,
        qualname=f"{prefix}.{node.name}",
        params=_params(node, parse_doc_section(docstring, ("Args", "Arguments", "Parameters"))),
        returns=_unparse(node.returns),
        docstring=docstring,
        decorators=decorators,
        is_property=any(d == "property" or d.endswith(".getter") for d in decorators),
        is_async=isinstance(node, ast.AsyncFunctionDef),
        lineno=node.lineno,
    )


def _attribute_doc(body: list[ast.stmt], index: int) -> str | None:
    """PEP 257 attribute docstring: a bare string literal right after an assignment."""
    if index + 1 >= len(body):
        return None
    nxt = body[index + 1]
    if isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant) and isinstance(nxt.value.value, str):
        return " ".join(nxt.value.value.split())
    return None


def _class(node: ast.ClassDef, prefix: str) -> ClassInfo:
    qualname = f"{prefix}.{node.name}"
    docstring = ast.get_docstring(node)
    field_docs = parse_doc_section(docstring, ("Attributes", "Fields"))
    decorators = [ast.unparse(d) for d in node.decorator_list]
    info = ClassInfo(
        name=node.name,
        qualname=qualname,
        bases=[ast.unparse(b) for b in node.bases],
        docstring=docstring,
        is_dataclass=any("dataclass" in d for d in decorators),
        lineno=node.lineno,
    )
    for index, stmt in enumerate(node.body):
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name.startswith("_"):
                continue
            info.fields.append(
                FieldInfo(
                    name=name,
                    annotation=_unparse(stmt.annotation),
                    default=_unparse(stmt.value),
                    doc=field_docs.get(name) or _attribute_doc(node.body, index),
                )
            )
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            if stmt.name == "__init__":
                info.init = _callable(stmt, qualname)
            elif not stmt.name.startswith("_"):
                info.methods.append(_callable(stmt, qualname))
    return info


def _constants(tree: ast.Module, module_name: str) -> list[ConstantInfo]:
    found: list[ConstantInfo] = []
    for index, stmt in enumerate(tree.body):
        target: ast.expr | None = None
        value: ast.expr | None = None
        annotation: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            target, value, annotation = stmt.target, stmt.value, stmt.annotation
        if not isinstance(target, ast.Name) or value is None:
            continue
        name = target.id
        if name.startswith("_") or not name.isupper():
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
        if not isinstance(literal, bool | int | float | str | tuple | list):
            continue
        found.append(
            ConstantInfo(
                name=name,
                qualname=f"{module_name}.{name}",
                value=list(literal) if isinstance(literal, tuple) else literal,
                annotation=_unparse(annotation),
                doc=_attribute_doc(tree.body, index),
            )
        )
    return found


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or root.name


def scan_module(root: Path, path: Path) -> ModuleInfo:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    name = _module_name(root, path)
    info = ModuleInfo(
        name=name,
        path=path.relative_to(root).as_posix(),
        docstring=ast.get_docstring(tree),
        constants=_constants(tree, name),
    )
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and not stmt.name.startswith("_"):
            info.classes.append(_class(stmt, name))
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and not stmt.name.startswith("_"):
            info.functions.append(_callable(stmt, name))
    return info


def _python_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        files.append(path)
    return files


def _documents(root: Path) -> list[DocumentInfo]:
    docs = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.lower() in _DOC_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            docs.append(
                DocumentInfo(
                    path=path.name,
                    text=text[:_MAX_DOC_CHARS],
                    truncated=len(text) > _MAX_DOC_CHARS,
                )
            )
    return docs


def scan_sdk(sdk_path: Path) -> SdkSnapshot:
    """Statically describe the SDK found at ``sdk_path`` (no code is executed)."""
    root = sdk_path.resolve()
    if not root.is_dir():
        raise ScanError(f"SDK path {sdk_path} is not a directory")
    files = _python_files(root)
    if not files:
        raise ScanError(f"SDK path {sdk_path} contains no Python files")
    packages = sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file() and child.name not in _SKIP_DIRS
    )
    snapshot = SdkSnapshot(
        sdk_name=root.name,
        packages=packages,
        modules=[scan_module(root, path) for path in files],
        documents=_documents(root),
    )
    snapshot.recompute_stats()
    return snapshot


def discover(sdk_path: Path, *, runtime: bool = True, timeout_s: float = 30.0) -> SdkSnapshot:
    """Static scan plus (optionally) the policy-gated read-only runtime probe."""
    from bodyboot.discovery.runtime_probe import run_runtime_probe

    snapshot = scan_sdk(sdk_path)
    if runtime:
        snapshot.runtime = run_runtime_probe(sdk_path, snapshot, timeout_s=timeout_s)
    else:
        snapshot.runtime.reason = "runtime probing disabled by caller"
    snapshot.recompute_stats()
    return snapshot
