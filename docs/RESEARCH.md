# Research notes

## Question
Can a coding agent bootstrap its own embodiment — discover an unfamiliar robot SDK and
produce verified sensor, actuator, safety and agent-tool interfaces — so that integration
stops being a per-robot human engineering project?

## Claim of this milestone (and only this)
On one deterministic, fictional Go2-like SDK, an LLM given only a neutral snapshot inferred
six capability mappings including non-trivial argument order, units, sign and quaternion
conventions and a safety-relevant look-alike (`stop` vs. torque cut); a deterministic
compiler turned that into a native adapter and an external dimOS package; and an independent
verifier that the agent could not see confirmed all of it by execution. Observed once with
Claude Code 2.1.252 / `claude-opus-5[1m]`: 71/71 checks, first attempt, ~68 s, ~$0.39.

This is an existence proof on a fixture. It is **not** evidence about real SDKs or hardware.

## Design hypotheses
1. **Split semantics from synthesis.** Letting the model emit a small typed plan instead of
   code makes the result scoreable (symbol / unit / sign accuracy), auditable (evidence per
   mapping) and safe (no code injection; motion gating lives in templates the model never touches).
2. **Description before interpretation.** A discovery stage that refuses to map keeps the
   agent's contribution measurable and lets backends be swapped.
3. **Refusals are information.** Showing the agent what the probe declined to call, and why,
   is itself evidence about risk.
4. **Verification must not share a brain with generation.** Ground truth read directly from
   the SDK, physics sanity checks, and call spies do not depend on the plan being honest.

## Metrics the harness already yields
capability recall (n/6) · symbol accuracy · unit/sign accuracy (via executed contracts) ·
calibration (confidence vs. correctness) · attempts, latency, cost · probe refusals.

## Threats to validity
* The fixture was written by the same author as the tool; its docs are unusually clean.
* One robot class, one SDK shape (session + pull channels), Python only.
* The renamed-SDK test shows independence from *names*, not from *structure*.
* A single Claude run is an anecdote; no variance study yet.
* dimOS output is statically checked, never executed in dimOS.

## Future work (explicitly out of scope here)
Real SDKs (unitree_sdk2, WebRTC stacks) under replay transports · callback/streaming APIs ·
N-run evaluation with ablations (no runtime probe, no README, obfuscated names) ·
hardware-in-the-loop validation with staged motion gates (zero-velocity → tethered → free) ·
operational safety learned by the human baseline (watchdogs, deadman, velocity ownership,
mode arming) · simulation/replay contract generation. Not planned for BODYBOOT itself:
locomotion, SLAM, VLA training, RL, self-improving policies.

## Human baseline
`BeBecpp/DimOs_Windows` integrates a real Go2 by hand (~3.4k lines of adapter/connection/
control core, plus perception, service layer and ~330 tests). BODYBOOT compares capability
*categories* only (`baseline_comparison.md`), after generation, without reading its code into
the generator. The honest asymmetry: the human stack moves a real robot and encodes lessons
from doing so; BODYBOOT's output has only ever met a simulator.
