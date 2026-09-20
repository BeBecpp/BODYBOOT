"""Agent stage: JSON handling, the Claude CLI backend (with a fake CLI) and the heuristics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from bodyboot.agents import get_backend
from bodyboot.agents.base import (
    AgentError,
    AgentUnavailableError,
    InvalidAgentOutputError,
    extract_json_object,
    plan_from_agent_output,
)
from bodyboot.agents.claude_backend import (
    ClaudeAgentBackend,
    CliRun,
    build_command,
    find_claude_cli,
    parse_cli_help,
)
from bodyboot.agents.deterministic_backend import DeterministicAgentBackend, infer_unit
from bodyboot.agents.prompting import assert_no_evaluator_leak, build_prompt
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.discovery.scanner import discover

from .conftest import EVALUATOR, capability, make_renamed_sdk

MODERN_HELP = """Usage: claude [options] [prompt]
  -p, --print            Print response and exit
  --output-format <f>    "text", "json"
  --json-schema <schema> JSON Schema for structured output
  --system-prompt <p>    System prompt
  --tools <tools...>     Use "" to disable all tools
  --safe-mode            Disable customizations
  --no-session-persistence
  --model <model>
"""
LEGACY_HELP = "Usage: claude [options]\n  -p    print mode\n  --verbose\n"


class FakeCli:
    """Stands in for the claude binary; records every invocation."""

    def __init__(self, help_text: str, replies: list[CliRun]) -> None:
        self.help_text, self.replies, self.calls = help_text, list(replies), []

    def __call__(self, argv: list[str], stdin: str, cwd: Path, timeout: float) -> CliRun:
        if argv[1:] == ["--help"]:
            return CliRun(0, self.help_text, "")
        self.calls.append({"argv": argv, "stdin": stdin, "cwd_listing": os.listdir(cwd)})
        return self.replies.pop(0)


def envelope(plan: dict[str, Any], *, structured: bool) -> CliRun:
    body: dict[str, Any] = {"type": "result", "is_error": False, "total_cost_usd": 0.1,
                            "modelUsage": {"claude-test": {}}}  # fmt: skip
    if structured:
        body.update(result="", structured_output=plan)
    else:
        body["result"] = f"Here is the plan:\n```json\n{json.dumps(plan)}\n```"
    return CliRun(0, json.dumps(body), "")


def test_extract_json_tolerates_fences_and_chatter() -> None:
    assert extract_json_object('Sure!\n```json\n{"a": {"b": 1}}\n```\nDone.') == {"a": {"b": 1}}
    assert extract_json_object('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("raw", ["", "no json here", "{broken", "[1, 2, 3]"])
def test_invalid_agent_json_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidAgentOutputError):
        plan_from_agent_output(raw)


def test_valid_json_with_wrong_shape_is_rejected() -> None:
    with pytest.raises(InvalidAgentOutputError, match="not a valid embodiment plan"):
        plan_from_agent_output('{"robot_family": "dog", "capabilities": []}')


def test_cli_help_is_parsed_not_assumed() -> None:
    modern, legacy = parse_cli_help(MODERN_HELP), parse_cli_help(LEGACY_HELP)
    assert modern.print_flag == "--print" and modern.has("--json-schema") and modern.has("--tools")
    assert legacy.print_flag == "-p" and not legacy.has("--json-schema")
    argv, notes = build_command(Path("claude"), legacy, use_schema=True)
    assert argv == ["claude", "-p"], "must not pass flags the installed CLI does not advertise"
    assert notes["system_prompt_via"] == "stdin" and notes["json_schema_flag"] is False
    argv, notes = build_command(Path("claude"), modern, use_schema=True)
    assert argv[argv.index("--tools") + 1] == "" and "--safe-mode" in argv and "--json-schema" in argv


def test_cli_without_print_mode_is_unavailable() -> None:
    with pytest.raises(AgentUnavailableError):
        build_command(Path("claude"), parse_cli_help("Usage: claude\n  --verbose\n"), use_schema=True)


def test_missing_binary_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BODYBOOT_CLAUDE_BIN", str(tmp_path / "nope.exe"))
    assert find_claude_cli() is None


def test_claude_backend_accepts_structured_output_and_audits(
    snapshot: SdkSnapshot, plan_dict: dict[str, Any], tmp_path: Path
) -> None:
    cli = FakeCli(MODERN_HELP, [envelope(plan_dict, structured=True)])
    result = ClaudeAgentBackend(Path("claude"), runner=cli).analyze(snapshot, tmp_path)
    assert result.attempts == 1 and result.model == "claude-test" and len(result.plan.capabilities) == 6
    assert cli.calls[0]["cwd_listing"] == [], "the agent must run in an empty directory"
    for name in ("prompt.txt", "raw_response.json", "cli_help.txt", "audit.json",
                 "attempt_1_command.json"):  # fmt: skip
        assert (tmp_path / name).is_file(), name
    assert (tmp_path / "prompt.txt").read_text(encoding="utf-8") == cli.calls[0]["stdin"]


def test_claude_prompt_contains_snapshot_and_schema_but_never_the_evaluator(
    snapshot: SdkSnapshot, plan_dict: dict[str, Any], tmp_path: Path
) -> None:
    cli = FakeCli(MODERN_HELP, [envelope(plan_dict, structured=True)])
    ClaudeAgentBackend(Path("claude"), runner=cli).analyze(snapshot, tmp_path)
    sent = cli.calls[0]["stdin"] + " ".join(cli.calls[0]["argv"])
    assert "mysterybot.sport.SportChannel.joystick" in sent and "canonical_name" in sent
    canary = json.loads(EVALUATOR.read_text(encoding="utf-8"))["canary"]
    assert canary not in sent and "expected_capabilities" not in sent
    # The snapshot describes; the answer key (which symbol is the IMU etc.) must not be in it.
    assert '"vendor_symbol": "mysterybot' not in sent


def test_leak_guard_blocks_evaluator_material(snapshot: SdkSnapshot) -> None:
    with pytest.raises(AssertionError, match="evaluator"):
        assert_no_evaluator_leak("see fixtures/evaluator/expected_capabilities.json")
    assert_no_evaluator_leak(build_prompt(snapshot))


def test_claude_backend_repairs_after_invalid_json(
    snapshot: SdkSnapshot, plan_dict: dict[str, Any], tmp_path: Path
) -> None:
    garbage = CliRun(0, json.dumps({"type": "result", "is_error": False, "result": "I think..."}), "")
    cli = FakeCli(MODERN_HELP, [garbage, envelope(plan_dict, structured=False)])
    result = ClaudeAgentBackend(Path("claude"), runner=cli).analyze(snapshot, tmp_path)
    assert result.attempts == 2
    assert "previous answer was rejected" in cli.calls[1]["stdin"]
    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert [a["status"] for a in audit["attempts"]] == ["failed", "accepted"]


def test_claude_backend_drops_schema_flag_after_cli_error(
    snapshot: SdkSnapshot, plan_dict: dict[str, Any], tmp_path: Path
) -> None:
    cli = FakeCli(MODERN_HELP, [CliRun(1, "", "unknown schema keyword"), envelope(plan_dict, structured=False)])
    ClaudeAgentBackend(Path("claude"), runner=cli).analyze(snapshot, tmp_path)
    assert "--json-schema" in cli.calls[0]["argv"] and "--json-schema" not in cli.calls[1]["argv"]


def test_claude_backend_gives_up_honestly(snapshot: SdkSnapshot, tmp_path: Path) -> None:
    cli = FakeCli(MODERN_HELP, [CliRun(0, "not json at all", "")] * 3)
    with pytest.raises(AgentError, match="no valid plan in 3 attempts"):
        ClaudeAgentBackend(Path("claude"), runner=cli).analyze(snapshot, tmp_path)
    assert len(cli.calls) == 3


def test_unknown_backend_name() -> None:
    with pytest.raises(AgentError):
        get_backend("gpt")


def test_unit_inference_from_names_and_docs() -> None:
    assert infer_unit("accel", "accel_g", None) == (9.80665, "g")
    assert infer_unit("time", "t_us", None) == (1e-6, "us")
    assert infer_unit("angular_rate", "spin", "Rate in degrees per second.")[1] == "deg/s"  # type: ignore[index]
    assert infer_unit("angle", "heading", "Yaw in radians.") == (1.0, "rad")
    assert infer_unit("length", "mystery", "no unit given") is None


def test_deterministic_backend_infers_the_tricky_conventions(plan_dict: dict[str, Any]) -> None:
    params = {p["vendor_param"]: p for p in capability(plan_dict, "locomotion.velocity")["parameters"]}
    assert [params[v]["canonical_source"] for v in ("v1", "v2", "v3")] == ["vy", "vx", "yaw_rate"]
    assert params["v3"]["scale"] == pytest.approx(-57.29577951308232)
    assert (params["v2"]["vendor_min"], params["v2"]["vendor_max"]) == (-1.2, 1.2)
    quat = next(f for f in capability(plan_dict, "imu")["fields"] if f["canonical_field"] == "orientation_xyzw")
    assert quat["indices"] == [1, 2, 3, 0]
    assert capability(plan_dict, "locomotion.stop")["vendor_symbol"].endswith(".freeze")
    assert all(c["evidence"] for c in plan_dict["capabilities"])


def test_deterministic_backend_generalises_to_a_renamed_sdk(tmp_path: Path) -> None:
    sdk = make_renamed_sdk(tmp_path)
    plan = DeterministicAgentBackend().analyze(discover(sdk), tmp_path / "audit").plan
    symbols = {c.canonical_name: c.vendor_symbol for c in plan.capabilities}
    assert symbols == {
        "camera.rgb": "acmerobot.camera.OpticFeed.grab_color",
        "imu": "acmerobot.state.TelemetryPort.get_inertial",
        "odometry": "acmerobot.state.TelemetryPort.get_body_state",
        "locomotion.velocity": "acmerobot.sport.GaitPort.walk_cmd",
        "locomotion.stop": "acmerobot.sport.GaitPort.halt",
        "connection.health": "acmerobot.client.BotLink.ping_link",
    }


def test_deterministic_backend_reports_what_it_cannot_find(tmp_path: Path) -> None:
    package = tmp_path / "sdk" / "tinybot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\nclass Ping:\n    ok: bool\n    rtt_ms: float\n\n"
        'class Link:\n    def __init__(self, url: str = "sim://x") -> None: ...\n'
        "    def open(self) -> None: ...\n"
        '    def ping(self) -> Ping:\n        """Read-only link health check."""\n        return Ping(True, 1.0)\n',
        encoding="utf-8",
    )
    plan = DeterministicAgentBackend().analyze(discover(tmp_path / "sdk"), tmp_path / "audit").plan
    assert plan.names() == ["connection.health"]
    assert any("locomotion.stop" in w for w in plan.warnings) and any("imu" in w for w in plan.warnings)


@pytest.mark.claude
@pytest.mark.skipif(os.environ.get("BODYBOOT_TEST_CLAUDE") != "1", reason="set BODYBOOT_TEST_CLAUDE=1")
def test_real_claude_cli_end_to_end(snapshot: SdkSnapshot, tmp_path: Path) -> None:
    result = get_backend("claude").analyze(snapshot, tmp_path)
    assert len(result.plan.capabilities) == 6
