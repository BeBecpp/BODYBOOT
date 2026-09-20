"""Agent backend that asks the locally installed Claude / Claude Code CLI.

Nothing about the CLI syntax is assumed: the backend runs ``claude --help``,
parses which flags the installed version really supports, and builds the
non-interactive invocation from those. The model gets no tools, runs in an
empty temporary directory, and receives only instructions + snapshot + schema.
The exact prompt, command line and raw response of every attempt are recorded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bodyboot.agents.base import (
    AgentBackend,
    AgentError,
    AgentResult,
    AgentUnavailableError,
    InvalidAgentOutputError,
    plan_from_agent_output,
)
from bodyboot.agents.prompting import (
    SYSTEM_PROMPT,
    assert_no_evaluator_leak,
    build_prompt,
    build_repair_prompt,
)
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.plan import plan_json_schema

ENV_BINARY = "BODYBOOT_CLAUDE_BIN"
ENV_MODEL = "BODYBOOT_CLAUDE_MODEL"
ENV_TIMEOUT = "BODYBOOT_CLAUDE_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 600.0
MAX_ATTEMPTS = 3
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*|-[a-zA-Z])(?![\w-])")


@dataclass(frozen=True)
class CliRun:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], str, Path, float], CliRun]


@dataclass(frozen=True)
class CliCapabilities:
    """Which flags the *installed* CLI advertises in its own --help output."""

    flags: frozenset[str]

    def has(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def print_flag(self) -> str | None:
        if self.has("--print"):
            return "--print"
        return "-p" if self.has("-p") else None


def parse_cli_help(help_text: str) -> CliCapabilities:
    return CliCapabilities(flags=frozenset(_FLAG.findall(help_text)))


def _candidate_binaries() -> list[Path]:
    home = Path.home()
    names = ["claude.exe", "claude.cmd", "claude"] if os.name == "nt" else ["claude"]
    roots = [home / ".local" / "bin", home / ".claude" / "local", home / ".claude" / "bin"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "npm")
    else:
        roots += [Path("/usr/local/bin"), Path("/opt/homebrew/bin"), home / ".npm-global" / "bin"]
    return [root / name for root in roots for name in names]


def find_claude_cli(explicit: str | None = None) -> Path | None:
    """Locate a Claude CLI: explicit path, $BODYBOOT_CLAUDE_BIN, PATH, then known locations."""
    for choice in (explicit, os.environ.get(ENV_BINARY)):
        if choice:
            path = Path(choice).expanduser()
            return path if path.is_file() else None
    found = shutil.which("claude")
    if found:
        return Path(found)
    for candidate in _candidate_binaries():
        if candidate.is_file():
            return candidate
    return None


def _subprocess_runner(argv: list[str], stdin_text: str, cwd: Path, timeout_s: float) -> CliRun:
    completed = subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        timeout=timeout_s,
        check=False,
    )
    return CliRun(completed.returncode, completed.stdout, completed.stderr)


def build_command(
    binary: Path,
    caps: CliCapabilities,
    *,
    use_schema: bool,
    model: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Build the non-interactive invocation from flags the installed CLI really has."""
    print_flag = caps.print_flag
    if print_flag is None:
        raise AgentUnavailableError("the installed Claude CLI advertises no non-interactive print mode (-p/--print)")
    # .cmd/.bat shims go through cmd.exe, which cannot carry JSON or multi-word arguments safely.
    rich_args = binary.suffix.lower() not in (".cmd", ".bat")
    argv = [str(binary), print_flag]
    notes: dict[str, Any] = {"system_prompt_via": "stdin", "tools": "not restricted by flag"}
    if caps.has("--output-format"):
        argv += ["--output-format", "json"]
    if caps.has("--tools"):
        argv += ["--tools", ""]
        notes["tools"] = "all built-in tools disabled (--tools '')"
    if caps.has("--safe-mode"):
        argv.append("--safe-mode")
    elif caps.has("--strict-mcp-config"):
        argv.append("--strict-mcp-config")
    if caps.has("--no-session-persistence"):
        argv.append("--no-session-persistence")
    if caps.has("--system-prompt") and rich_args:
        argv += ["--system-prompt", SYSTEM_PROMPT]
        notes["system_prompt_via"] = "--system-prompt"
    if model and caps.has("--model"):
        argv += ["--model", model]
    notes["json_schema_flag"] = bool(use_schema and caps.has("--json-schema") and rich_args)
    if notes["json_schema_flag"]:
        argv += ["--json-schema", json.dumps(plan_json_schema(), separators=(",", ":"))]
    return argv, notes


def decode_cli_output(stdout: str) -> tuple[Any, dict[str, Any]]:
    """Return (plan payload, metadata) from CLI stdout, with or without a JSON envelope."""
    text = stdout.strip()
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return text, {}
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        return envelope if isinstance(envelope, dict) else text, {}
    meta: dict[str, Any] = {
        "cost_usd": envelope.get("total_cost_usd"),
        "duration_ms": envelope.get("duration_ms"),
        "session_id": envelope.get("session_id"),
        "models": sorted((envelope.get("modelUsage") or {}).keys()),
    }
    if envelope.get("is_error"):
        raise AgentError(f"Claude CLI reported an error: {str(envelope.get('result'))[:500]}")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured, meta
    return str(envelope.get("result") or ""), meta


class ClaudeAgentBackend(AgentBackend):
    name = "claude"

    def __init__(
        self,
        binary: Path | None = None,
        *,
        model: str | None = None,
        timeout_s: float | None = None,
        runner: Runner | None = None,
    ) -> None:
        self._binary = binary
        self._model = model or os.environ.get(ENV_MODEL)
        self._timeout_s = timeout_s or float(os.environ.get(ENV_TIMEOUT, DEFAULT_TIMEOUT_S))
        self._run = runner or _subprocess_runner

    def _inspect_cli(self, binary: Path, cwd: Path) -> tuple[CliCapabilities, str]:
        try:
            run = self._run([str(binary), "--help"], "", cwd, 60.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentUnavailableError(f"could not run {binary} --help: {exc}") from exc
        help_text = run.stdout or run.stderr
        if run.returncode != 0 or not help_text.strip():
            raise AgentUnavailableError(f"{binary} --help failed (exit {run.returncode})")
        return parse_cli_help(help_text), help_text

    def analyze(self, snapshot: SdkSnapshot, audit_dir: Path) -> AgentResult:
        binary = self._binary or find_claude_cli()
        if binary is None:
            raise AgentUnavailableError(
                "no Claude CLI found (looked at $BODYBOOT_CLAUDE_BIN, PATH and the usual "
                "install locations). Use --agent deterministic, or install Claude Code."
            )
        audit_dir.mkdir(parents=True, exist_ok=True)
        base_prompt = build_prompt(snapshot)
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        prompt = base_prompt
        use_schema = True
        last_error = "no attempt made"

        with tempfile.TemporaryDirectory(prefix="bodyboot-agent-") as tmp:
            cwd = Path(tmp)  # empty directory: no project files, no CLAUDE.md, nothing to read
            caps, help_text = self._inspect_cli(binary, cwd)
            (audit_dir / "cli_help.txt").write_text(help_text, encoding="utf-8")

            for number in range(1, MAX_ATTEMPTS + 1):
                argv, notes = build_command(binary, caps, use_schema=use_schema, model=self._model)
                stdin_text = (
                    prompt if notes["system_prompt_via"] == "--system-prompt" else f"{SYSTEM_PROMPT}\n\n{prompt}"
                )
                assert_no_evaluator_leak(stdin_text)
                stem = f"attempt_{number}"
                (audit_dir / f"{stem}_prompt.txt").write_text(stdin_text, encoding="utf-8")
                (audit_dir / f"{stem}_command.json").write_text(
                    json.dumps({"argv": argv, "cwd": "<empty temp dir>", "notes": notes}, indent=2),
                    encoding="utf-8",
                )
                record: dict[str, Any] = {"attempt": number, "notes": notes, "status": "failed"}
                attempts.append(record)
                raw = ""
                try:
                    run = self._run(argv, stdin_text, cwd, self._timeout_s)
                    raw = run.stdout
                    (audit_dir / f"{stem}_raw_response.txt").write_text(
                        run.stdout + (f"\n--- stderr ---\n{run.stderr}" if run.stderr else ""),
                        encoding="utf-8",
                    )
                    if run.returncode != 0:
                        raise AgentError(f"Claude CLI exited with {run.returncode}: {run.stderr.strip()[:400]}")
                    payload, meta = decode_cli_output(run.stdout)
                    record["meta"] = meta
                    plan = plan_from_agent_output(payload)
                except subprocess.TimeoutExpired:
                    last_error = f"Claude CLI timed out after {self._timeout_s:.0f}s"
                    record["error"] = last_error
                    continue
                except InvalidAgentOutputError as exc:
                    last_error = str(exc)
                    record["error"] = last_error
                    prompt = build_repair_prompt(base_prompt, raw, last_error)
                    continue
                except (AgentError, OSError) as exc:
                    last_error = str(exc)
                    record["error"] = last_error
                    use_schema = False  # the structured-output flag is the likeliest culprit
                    continue

                record["status"] = "accepted"
                (audit_dir / "prompt.txt").write_text(stdin_text, encoding="utf-8")
                (audit_dir / "raw_response.json").write_text(run.stdout, encoding="utf-8")
                self._write_index(audit_dir, binary, attempts)
                models = meta.get("models") or []
                return AgentResult(
                    plan=plan,
                    backend=self.name,
                    model=", ".join(models) or self._model,
                    attempts=number,
                    duration_s=time.perf_counter() - started,
                    cost_usd=meta.get("cost_usd"),
                    audit_files=sorted(p.name for p in audit_dir.iterdir()),
                )

        self._write_index(audit_dir, binary, attempts)
        raise AgentError(f"Claude produced no valid plan in {MAX_ATTEMPTS} attempts: {last_error}")

    @staticmethod
    def _write_index(audit_dir: Path, binary: Path, attempts: list[dict[str, Any]]) -> None:
        (audit_dir / "audit.json").write_text(
            json.dumps({"backend": "claude", "binary": str(binary), "attempts": attempts}, indent=2),
            encoding="utf-8",
        )


__all__ = [
    "ClaudeAgentBackend",
    "CliCapabilities",
    "CliRun",
    "build_command",
    "decode_cli_output",
    "find_claude_cli",
    "parse_cli_help",
]
