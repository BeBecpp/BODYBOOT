# CLAUDE.md — BODYBOOT

## Purpose
BODYBOOT is an embodiment compiler: given an unfamiliar robot vendor SDK it discovers the
surface, lets an agent infer semantics, deterministically compiles a native adapter and an
external dimOS plugin, and independently verifies them. *"Give the AI a robot, not a robot driver."*

## Architecture (src/bodyboot)
`discover → analyze → compile → verify → report`, each stage reading/writing
`.bodyboot/runs/<run-id>/`. Orchestrated by `pipeline.py`, exposed by `cli.py`.

| Module | Role |
|---|---|
| `canonical.py` | Target contract (6 capabilities, SI units, REP-103). No vendor knowledge. |
| `plan.py` | `EmbodimentPlan` schema — the only thing an agent may produce. Validates identifiers. |
| `discovery/` | `scanner.py` pure AST (never imports the SDK); `probe_worker.py` subprocess probe. |
| `safety/policy.py` | `REAL_MOTION_ENABLED=False`, probe allow/deny rules, plan safety rules. |
| `agents/` | `AgentBackend` → `claude_backend.py` (parses real `--help`), `deterministic_backend.py`. |
| `compiler/` | Jinja templates → `generated/native_<id>/`, `generated/dimos_<id>/`. No model involved. |
| `verification/` | `native_runner.py` (subprocess + spies), `contracts.py`, `verifier.py`, `report.py`. |

Key rule: **AI decides semantics; the deterministic compiler writes code.**

## Immutable safety rules
1. Never send a real non-zero motion command. Generated `velocity()` must make zero vendor calls.
2. `REAL_MOTION_ENABLED` stays the literal `False`; add no flag, env var or plan field that flips it.
3. Runtime probing: read-only, zero-argument, simulated transport only. Deny beats allow.
4. No stop capability ⇒ no motion surface.
5. **Never fake hardware validation.** Never print, log or document that real hardware passed
   without evidence from that exact robot. Fixture results are fixture results.
6. The evaluator file `fixtures/evaluator/expected_capabilities.json` must never reach an agent
   prompt, a plan or generated code. Only `verification/` may read it.
7. Never modify `dimensionalOS/dimos` or `BeBecpp/DimOs_Windows`; never write `all_blueprints.py`.
   External dimOS packages register via the `dimos.blueprints` entry-point group.

## Don't
* Don't hardcode fixture names (`mysterybot`, `joystick`, …) anywhere in `src/` — a test enforces it.
* Don't prewrite plans or adapters; don't print success that wasn't computed.
* Don't weaken a contract to get green. If the verifier is wrong, fix the verifier and say why.
* Don't let agent free text into generated source except via `repr` or `safe_text`.

## Commands
```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run bodyboot demo --agent deterministic
uv run bodyboot demo --agent claude        # needs a logged-in Claude CLI
```
(`python -m uv …` works if uv is not on PATH.)

## Definition of done
`uv sync` clean → `pytest` green → both demos end `BODYBOOT VERIFIED` → plan, adapter and
plugin produced at runtime → `report.html` written → `git status` shows no `.bodyboot/`,
`generated/*` or secrets → claims in docs match executed evidence.
