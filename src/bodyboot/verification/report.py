"""Standalone HTML report for one run (inline CSS + inline SVG, no JavaScript, no assets)."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

from bodyboot import TAGLINE, __version__
from bodyboot.canonical import CAPABILITIES
from bodyboot.discovery.models import SdkSnapshot
from bodyboot.plan import EmbodimentPlan, load_plan
from bodyboot.runs import RunPaths
from bodyboot.safety.policy import is_exception_class

_RISK_COLOR = {"read_only": "#2f9e6e", "motion": "#d9822b", "stop": "#c0392b"}

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16202c;--mut:#5d6b7a;--line:#dfe4ea;--acc:#2456d6;
--ok:#2f9e6e;--bad:#c0392b;--warn:#d9822b;--code:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f141a;--card:#171e27;--ink:#e6ebf1;--mut:#93a1b1;
--line:#2a3441;--acc:#7aa2ff;--ok:#4cc38a;--bad:#ff6b5e;--warn:#f0a351;--code:#1f2833}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:30px;margin:0;letter-spacing:.04em}h2{font-size:18px;margin:0 0 12px}
.tag{color:var(--mut);font-style:italic;margin:2px 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:16px 0}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;
letter-spacing:.04em;color:#fff;background:var(--mut)}
.b-ok{background:var(--ok)}.b-bad{background:var(--bad)}.b-warn{background:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:22px}.stat span{color:var(--mut);font-size:12px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
code,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}
code{background:var(--code);padding:1px 5px;border-radius:5px;word-break:break-word}
.bar{height:7px;border-radius:4px;background:var(--line);min-width:70px}
.bar i{display:block;height:100%;border-radius:4px;background:var(--acc)}
details{border-top:1px solid var(--line);padding:9px 0}summary{cursor:pointer;font-weight:600}
ul{margin:8px 0 0;padding-left:20px}li{margin:3px 0}.mut{color:var(--mut)}
.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
svg text{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;fill:var(--ink)}
svg .hdr{font-weight:700;font-family:system-ui,sans-serif;font-size:12px}
svg .dim{fill:var(--mut)}svg rect.box{fill:var(--card);stroke:var(--line)}
footer{color:var(--mut);font-size:12.5px;margin-top:26px}
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {kind}">{_e(text)}</span>'


def _graph(snapshot: SdkSnapshot, plan: EmbodimentPlan) -> str:
    """Discovery graph: vendor handle classes and methods on the left, canonical on the right."""
    mapped = {cap.vendor_symbol: cap for cap in plan.capabilities}
    skipped = {o.symbol: o.reason for o in snapshot.runtime.observations if o.status == "skipped"}
    row_h, left_x, left_w, right_x, right_w = 21, 12, 400, 700, 250
    y = 14
    anchors: dict[str, int] = {}
    parts: list[str] = []
    for cls in snapshot.all_classes():
        if cls.is_dataclass or is_exception_class(cls) or not cls.public_methods():
            continue
        height = row_h * (len(cls.public_methods()) + 1) + 8
        parts.append(f'<rect class="box" x="{left_x}" y="{y}" width="{left_w}" height="{height}" rx="8"/>')
        parts.append(f'<text class="hdr" x="{left_x + 10}" y="{y + 16}">{_e(cls.qualname)}</text>')
        for index, method in enumerate(cls.public_methods(), start=1):
            ty = y + 16 + index * row_h
            cap = mapped.get(method.qualname)
            if cap is not None:
                anchors[method.qualname] = ty - 4
                color = _RISK_COLOR[cap.risk]
                parts.append(f'<circle cx="{left_x + 16}" cy="{ty - 4}" r="4" fill="{color}"/>')
                parts.append(f'<text x="{left_x + 28}" y="{ty}" font-weight="700">{_e(method.signature())[:52]}</text>')
            else:
                note = "  (not probed)" if method.qualname in skipped else ""
                parts.append(f'<text class="dim" x="{left_x + 28}" y="{ty}">{_e(method.signature())[:44]}{note}</text>')
        y += height + 10
    total_h = max(y, 60 + 58 * len(CAPABILITIES))
    gap = (total_h - 28) / len(CAPABILITIES)
    for index, spec in enumerate(CAPABILITIES):
        cy = 14 + gap * index + gap / 2
        cap = plan.get(spec.name)
        color = _RISK_COLOR[spec.risk]
        parts.append(
            f'<rect class="box" x="{right_x}" y="{cy - 19}" width="{right_w}" height="38" rx="8" '
            f'style="stroke:{color}"/>'
        )
        parts.append(f'<text class="hdr" x="{right_x + 12}" y="{cy - 2}">{_e(spec.name)}</text>')
        label = f"{spec.risk} · {cap.confidence:.2f}" if cap else "UNMAPPED"
        parts.append(f'<text class="dim" x="{right_x + 12}" y="{cy + 13}">{_e(label)}</text>')
        if cap is not None and cap.vendor_symbol in anchors:
            sy, sx = anchors[cap.vendor_symbol], left_x + left_w
            mid = (sx + right_x) / 2
            parts.append(
                f'<path d="M{sx},{sy} C{mid},{sy} {mid},{cy} {right_x},{cy}" fill="none" '
                f'stroke="{color}" stroke-width="2" opacity=".8"/>'
            )
    return (
        f'<svg viewBox="0 0 {right_x + right_w + 12} {total_h}" width="100%" role="img" '
        f'aria-label="Discovery graph">{"".join(parts)}</svg>'
    )


def _capability_rows(plan: EmbodimentPlan) -> str:
    rows = []
    for spec in CAPABILITIES:
        cap = plan.get(spec.name)
        if cap is None:
            rows.append(f"<tr><td><code>{_e(spec.name)}</code></td><td colspan=5 class=fail>not mapped</td></tr>")
            continue
        path = " → ".join(["session", *[f"{p}()" for p in cap.access_path]])
        rows.append(
            f"<tr><td><code>{_e(spec.name)}</code></td><td><code>{_e(cap.vendor_symbol)}</code>"
            f'<div class="mut mono">{_e(path)}</div></td>'
            f'<td><span class="badge" style="background:{_RISK_COLOR[cap.risk]}">{_e(cap.risk)}</span></td>'
            f'<td><div class="bar"><i style="width:{cap.confidence * 100:.0f}%"></i></div>'
            f'<span class="mono">{cap.confidence:.2f}</span></td><td>{_e(cap.frame or "-")}</td></tr>'
        )
    return "".join(rows)


def _conversion_rows(plan: EmbodimentPlan) -> str:
    rows = []
    for cap in plan.capabilities:
        for src in cap.fields:
            vendor = src.constant if src.constant is not None else ", ".join(src.vendor_paths)
            extra = f" idx {src.indices}" if src.indices else ""
            rows.append(
                f"<tr><td><code>{_e(cap.canonical_name)}.{_e(src.canonical_field)}</code></td>"
                f"<td><code>{_e(vendor)}</code>{_e(extra)}</td><td>{_e(src.vendor_unit or '-')}</td>"
                f'<td class="mono">× {src.scale:g}</td></tr>'
            )
        for prm in cap.parameters:
            limits = f"[{prm.vendor_min}, {prm.vendor_max}]" if prm.vendor_max is not None else "-"
            rows.append(
                f"<tr><td><code>{_e(cap.canonical_name)}</code> vendor arg <code>{_e(prm.vendor_param)}</code></td>"
                f"<td>← <code>{_e(prm.canonical_source or prm.constant)}</code> limits {_e(limits)}</td>"
                f'<td>{_e(prm.vendor_unit or "-")}</td><td class="mono">× {prm.scale:g}</td></tr>'
            )
    return "".join(rows)


def render_report(paths: RunPaths) -> str:
    manifest = paths.read_manifest()
    snapshot = SdkSnapshot.model_validate(json.loads(paths.snapshot.read_text(encoding="utf-8")))
    plan = load_plan(paths.plan)
    verification: dict[str, Any] = (
        json.loads(paths.verification.read_text(encoding="utf-8")) if paths.verification.is_file() else {}
    )
    status = verification.get("status", "NOT VERIFIED")
    kind = {"VERIFIED": "b-ok", "FAILED": "b-bad"}.get(status, "b-warn")
    stats = snapshot.stats

    evidence = "".join(
        f"<details><summary><code>{_e(cap.canonical_name)}</code> → <code>{_e(cap.vendor_symbol)}</code> "
        f'<span class="mut">({len(cap.evidence)} items)</span></summary><ul>'
        + "".join(f"<li>{_e(item)}</li>" for item in cap.evidence)
        + "</ul></details>"
        for cap in plan.capabilities
    )
    warnings = "".join(f"<li>{_e(w)}</li>" for w in plan.warnings) or '<li class="mut">none</li>'
    probe_rows = "".join(
        f"<tr><td><code>{_e(o.symbol)}</code></td><td>{_e(o.policy_class)}</td>"
        f'<td class="{"pass" if o.status == "ok" else "mut"}">{_e(o.status)}</td><td>{_e(o.reason)}</td></tr>'
        for o in snapshot.runtime.observations
    )
    groups = "".join(
        f'<div class="stat"><b class="{"pass" if g["status"] == "PASS" else "fail"}">{g["passed"]}/{g["total"]}</b>'
        f"<span>{_e(g['title'])} · {_e(g['status'])}</span></div>"
        for g in verification.get("groups", [])
    )
    checks = "".join(
        f"<tr><td>{_e(c['group'])}</td><td>{_e(c['name'])}</td>"
        f'<td class="{"pass" if c["passed"] else ("fail" if c["passed"] is False else "mut")}">'
        f"{'PASS' if c['passed'] else ('FAIL' if c['passed'] is False else 'UNSCORED')}</td>"
        f'<td class="mut">{_e(c["detail"])}</td></tr>'
        for c in verification.get("checks", [])
    )
    files = "".join(f"<li><code>{_e(f)}</code></li>" for f in manifest.get("generated_files", []))
    notes = "".join(f"<li>{_e(n)}</li>" for n in verification.get("notes", []))
    agent = _e(manifest.get("agent", "?")) + (
        f" · {_e(manifest['agent_model'])}" if manifest.get("agent_model") else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BODYBOOT report {_e(paths.run_id)}</title><style>{_CSS}</style></head><body><main>
<h1>BODYBOOT</h1><p class="tag">“{_e(TAGLINE)}”</p>
<div class="row">{_badge(status, kind)}{_badge("REAL MOTION DISABLED", "b-warn")}
{_badge("HARDWARE VALIDATED: NO")}<span class="mut mono">run {_e(paths.run_id)} · agent {agent}</span></div>

<section class="card"><h2>1 · SDK input</h2>
<p><code>{_e(manifest.get("sdk_path", "?"))}</code> · packages {_e(", ".join(snapshot.packages))}</p>
<div class="grid">
<div class="stat"><b>{stats.modules}</b><span>modules</span></div>
<div class="stat"><b>{stats.classes}</b><span>classes</span></div>
<div class="stat"><b>{stats.public_symbols}</b><span>public symbols</span></div>
<div class="stat"><b>{stats.constants}</b><span>constants</span></div>
<div class="stat"><b>{stats.runtime_calls}</b><span>read-only probe calls</span></div>
<div class="stat"><b>{stats.runtime_skipped}</b><span>calls refused by policy</span></div></div>
<p class="mut">Runtime probe: {_e(snapshot.runtime.reason)}. {_e(snapshot.runtime.transport_proof or "")}</p></section>

<section class="card"><h2>2 · Discovery graph</h2>
<p class="mut">Vendor handles and methods (left) → canonical capabilities (right).
<span style="color:{_RISK_COLOR["read_only"]}">●</span> read-only
<span style="color:{_RISK_COLOR["motion"]}">●</span> motion (mock-only)
<span style="color:{_RISK_COLOR["stop"]}">●</span> stop</p><div class="scroll">{_graph(snapshot, plan)}</div></section>

<section class="card"><h2>3 · Discovered capabilities</h2>
<p class="mut">Robot family: {_e(plan.robot_family)} · session <code>{_e(plan.connection.session_symbol)}</code></p>
<div class="scroll"><table><tr><th>canonical</th><th>vendor symbol</th><th>safety class</th>
<th>confidence</th><th>frame</th></tr>{_capability_rows(plan)}</table></div></section>

<section class="card"><h2>4 · Inferred conventions (units, order, sign)</h2>
<div class="scroll"><table><tr><th>canonical</th><th>vendor</th><th>vendor unit</th><th>scale</th></tr>
{_conversion_rows(plan)}</table></div></section>

<section class="card"><h2>5 · Evidence</h2>{evidence}
<h2 style="margin-top:16px">Agent warnings</h2><ul>{warnings}</ul></section>

<section class="card"><h2>6 · Safety: what the probe touched and refused</h2>
<div class="scroll"><table><tr><th>vendor symbol</th><th>policy class</th><th>status</th><th>reason</th></tr>
{probe_rows}</table></div></section>

<section class="card"><h2>7 · Generated files</h2><ul>{files}</ul></section>

<section class="card"><h2>8 · Independent verification — {_badge(status, kind)}</h2>
<div class="grid">{groups}</div>
<p><b>{verification.get("passed", 0)}/{verification.get("total", 0)}</b> checks passed · Real motion
<b>{_e(verification.get("real_motion", "DISABLED"))}</b></p><ul>{notes}</ul>
<div class="scroll"><table><tr><th>group</th><th>check</th><th>result</th><th>detail</th></tr>{checks}</table></div>
</section>
<footer>Generated by BODYBOOT {__version__}. Semantics were decided by the agent; all code was written by the
deterministic compiler; verification executed independently of the agent's claims.</footer>
</main></body></html>
"""


def write_report(paths: RunPaths) -> Path:
    paths.report.write_text(render_report(paths), encoding="utf-8")
    paths.update_manifest(report=str(paths.report))
    return paths.report
