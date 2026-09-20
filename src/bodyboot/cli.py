"""BODYBOOT command line: discover -> analyze -> compile -> verify -> report (or `demo`)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from bodyboot import TAGLINE, __version__, pipeline
from bodyboot.agents import AGENT_NAMES, AgentError, AgentResult
from bodyboot.baseline import write_baseline_comparison
from bodyboot.compiler import CompileError, CompileResult
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.discovery.scanner import ScanError
from bodyboot.plan import InvalidPlanError
from bodyboot.runs import (
    RunError,
    RunPaths,
    default_fixture_sdk,
    find_workspace,
    new_run_id,
    open_run,
    resolve_run_id,
    run_id_from_plan_path,
)
from bodyboot.verification.verifier import STATUS_VERIFIED, VerificationResult

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=f"BODYBOOT {__version__} - self-bootstrapping embodiment compiler. {TAGLINE}",
)
console = Console(highlight=False)

WorkspaceOpt = Annotated[Path | None, typer.Option("--workspace", help="Where .bodyboot/ and generated/ live.")]
AgentOpt = Annotated[str, typer.Option("--agent", help=f"Agent backend: {' | '.join(AGENT_NAMES)}")]


def _workspace(value: Path | None) -> Path:
    return value.resolve() if value else find_workspace()


def _fail(message: str) -> typer.Exit:
    console.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code=1)


def _open(workspace: Path, run_id: str | None) -> RunPaths:
    try:
        return open_run(workspace, resolve_run_id(workspace, run_id))
    except RunError as exc:
        raise _fail(str(exc)) from exc


def _print_discovery(snapshot: SdkSnapshot) -> None:
    s = snapshot.stats
    console.print(f"      {s.modules} modules, {s.classes} classes, {s.public_symbols} public symbols, "
                  f"{s.constants} constants")  # fmt: skip
    if snapshot.runtime.executed:
        console.print(f"      read-only probe: {s.runtime_calls} calls made, "
                      f"[yellow]{s.runtime_skipped} refused by safety policy[/]")  # fmt: skip
    else:
        console.print(f"      runtime probe skipped: {snapshot.runtime.reason}")


def _print_plan(result: AgentResult) -> None:
    for cap in result.plan.capabilities:
        symbol = ".".join(cap.vendor_symbol.split(".")[-2:])
        color = "green" if cap.confidence >= 0.8 else "yellow"
        console.print(f"      {cap.canonical_name:<20} → {symbol:<32} [{color}]{cap.confidence:.2f}[/]")
    extra = f" · {result.model}" if result.model else ""
    console.print(f"      {len(result.plan.capabilities)} capabilities found "
                  f"[dim]({result.backend}{extra}, {result.duration_s:.1f}s, "
                  f"{result.attempts} attempt(s))[/]")  # fmt: skip
    for warning in result.plan.warnings[:6]:
        console.print(f"      [yellow]![/] [dim]{warning}[/]")


def _print_compile(result: CompileResult) -> None:
    console.print(f"      native adapter            [green]✓[/]  [dim]{result.native_dir.name}[/]")
    console.print(f"      dimOS external plugin     [green]✓[/]  [dim]{result.dimos_dir.name}[/]")
    for name, reason in result.withheld.items():
        console.print(f"      [yellow]withheld[/] {name}: {reason}")


def _print_scorecard(result: VerificationResult) -> None:
    table = Table(title="BODYBOOT VERIFICATION", title_justify="left", box=None, pad_edge=False)
    table.add_column("")
    table.add_column("", justify="right")
    table.add_column("")
    for group in result.groups():
        color = {"PASS": "green", "FAIL": "red"}.get(group.status, "yellow")
        table.add_row(group.title, f"{group.passed}/{group.total}", f"[{color}]{group.status}[/]")
    table.add_row("Real motion", "", f"[yellow]{result.real_motion}[/]")
    console.print(table)
    for check in result.failures():
        console.print(f"  [red]✗[/] {check.group}: {check.name} [dim]{check.detail}[/]")
    color = "green" if result.status == STATUS_VERIFIED else "red"
    console.print(f"\n[bold {color}]STATUS: {result.status}[/]")


@app.command()
def discover(
    sdk_path: Annotated[Path, typer.Argument(help="Path to the unfamiliar vendor SDK.")],
    workspace: WorkspaceOpt = None,
    no_runtime: Annotated[bool, typer.Option("--no-runtime", help="Static scan only.")] = False,
) -> None:
    """Inspect an SDK (static scan + policy-gated read-only probe) -> sdk_snapshot.json."""
    try:
        paths, snapshot = pipeline.stage_discover(_workspace(workspace), sdk_path, runtime=not no_runtime)
    except ScanError as exc:
        raise _fail(str(exc)) from exc
    _print_discovery(snapshot)
    console.print(f"run id: [bold]{paths.run_id}[/]\nsnapshot: {paths.snapshot}")


@app.command()
def analyze(
    run_id: Annotated[str | None, typer.Argument(help="Run id (default: latest).")] = None,
    agent: AgentOpt = "deterministic",
    workspace: WorkspaceOpt = None,
) -> None:
    """Ask an agent backend to infer the embodiment -> embodiment_plan.json."""
    paths = _open(_workspace(workspace), run_id)
    try:
        result = pipeline.stage_analyze(paths, agent)
    except AgentError as exc:
        raise _fail(str(exc)) from exc
    _print_plan(result)
    console.print(f"plan: {paths.plan}")


@app.command("compile")
def compile_(
    embodiment_plan: Annotated[Path, typer.Argument(help="Path to embodiment_plan.json.")],
    workspace: WorkspaceOpt = None,
) -> None:
    """Deterministically compile a plan into the native adapter and the dimOS plugin."""
    ws = _workspace(workspace)
    run_id = run_id_from_plan_path(embodiment_plan)
    paths = _open(ws, run_id) if run_id else RunPaths(ws, new_run_id()).ensure()
    if not paths.manifest.is_file():
        paths.update_manifest(note="compiled from a stand-alone plan")
    try:
        result = pipeline.stage_compile(paths, embodiment_plan)
    except (CompileError, InvalidPlanError) as exc:
        raise _fail(str(exc)) from exc
    _print_compile(result)
    console.print(f"run id: [bold]{paths.run_id}[/]")


@app.command()
def verify(
    run_id: Annotated[str | None, typer.Argument(help="Run id (default: latest).")] = None,
    expected: Annotated[Path | None, typer.Option(help="Evaluator ground truth JSON.")] = None,
    workspace: WorkspaceOpt = None,
) -> None:
    """Run the independent contracts against the generated embodiment."""
    paths = _open(_workspace(workspace), run_id)
    result = pipeline.stage_verify(paths, expected_path=expected)
    _print_scorecard(result)
    if result.status != STATUS_VERIFIED:
        raise typer.Exit(code=1)


@app.command()
def report(
    run_id: Annotated[str | None, typer.Argument(help="Run id (default: latest).")] = None,
    workspace: WorkspaceOpt = None,
) -> None:
    """Render the standalone HTML report for a run."""
    paths = _open(_workspace(workspace), run_id)
    console.print(f"report: {pipeline.stage_report(paths)}")


@app.command()
def baseline(
    run_id: Annotated[str | None, typer.Argument(help="Run id (default: latest).")] = None,
    workspace: WorkspaceOpt = None,
) -> None:
    """Write baseline_comparison.md (capability categories vs. the human DimOs_Windows work)."""
    paths = _open(_workspace(workspace), run_id)
    console.print(f"baseline comparison: {write_baseline_comparison(paths)}")


@app.command()
def demo(
    agent: AgentOpt = "deterministic",
    sdk: Annotated[Path | None, typer.Option(help="SDK to bootstrap (default: mystery fixture).")] = None,
    workspace: WorkspaceOpt = None,
) -> None:
    """Full pipeline on an unfamiliar SDK: discover → analyze → compile → verify → report."""
    ws = _workspace(workspace)
    sdk_path = sdk or default_fixture_sdk(ws)
    console.print(f"[bold]BODYBOOT[/] {__version__}  [dim]agent={agent}  sdk={sdk_path}[/]\n")
    try:
        console.print("[bold cyan][1/5][/] Inspecting unfamiliar vendor SDK...")
        paths, snapshot = pipeline.stage_discover(ws, sdk_path)
        _print_discovery(snapshot)

        console.print("\n[bold cyan][2/5][/] Asking coding agent to infer embodiment...")
        _print_plan(pipeline.stage_analyze(paths, agent))

        console.print("\n[bold cyan][3/5][/] Compiling embodiment...")
        _print_compile(pipeline.stage_compile(paths))

        console.print("\n[bold cyan][4/5][/] Running independent contracts...")
        result = pipeline.stage_verify(paths)
        console.print(f"      {result.passed}/{result.total} checks passed\n")
        _print_scorecard(result)
        report_path = pipeline.stage_report(paths)
        write_baseline_comparison(paths)
    except (ScanError, AgentError, CompileError, InvalidPlanError) as exc:
        raise _fail(str(exc)) from exc

    verified = result.status == STATUS_VERIFIED
    banner = "BODYBOOT VERIFIED" if verified else f"BODYBOOT {result.status}"
    console.print(f"\n[bold cyan][5/5][/] [bold {'green' if verified else 'red'}]{banner}[/]")
    console.print(f"      run     {paths.run_id}")
    console.print(f"      plan    {paths.plan}")
    console.print(f"      report  {report_path}")
    console.print(f'\n[italic]"{TAGLINE}"[/]')
    if not verified:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
