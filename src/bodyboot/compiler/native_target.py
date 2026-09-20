"""Target 1: the canonical native Python adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment

NATIVE_PACKAGE = "bodyboot_native_adapter"
_PACKAGE_FILES = ("__init__.py", "adapter.py", "safety.py", "types.py")


def render_package(env: Environment, context: dict[str, Any], package_dir: Path) -> list[Path]:
    """Render the adapter package into ``package_dir`` (also embedded by the dimOS target)."""
    package_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in _PACKAGE_FILES:
        path = package_dir / name
        path.write_text(env.get_template(f"native/{name}.j2").render(**context), encoding="utf-8")
        written.append(path)
    return written


def render_native(env: Environment, context: dict[str, Any], out_dir: Path) -> list[Path]:
    written = render_package(env, context, out_dir / NATIVE_PACKAGE)
    readme = out_dir / "README.md"
    readme.write_text(env.get_template("native/README.md.j2").render(**context), encoding="utf-8")
    return [*written, readme]
