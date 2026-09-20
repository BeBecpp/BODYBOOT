# Architecture

## Trust boundaries

```
 vendor SDK (untrusted code)        agent (untrusted judgement)        evaluator (secret)
        │                                   │                                 │
  AST scan: never imported                  │                                 │
  probe: separate process,                  │                                 │
  policy-gated, sim transport only          │                                 │
        ▼                                   ▼                                 │
  sdk_snapshot.json ───────────────▶ embodiment_plan.json                     │
  (describes, never maps)            (identifiers + numbers only,             │
                                      schema- and safety-validated)           │
                                            │                                 │
                                   deterministic compiler                     │
                                            ▼                                 ▼
                                   generated code ──────────────▶ verifier (separate process,
                                                                  spies on every vendor method)
```

Three things are untrusted and each gets its own containment:

1. **Vendor code** is never imported by BODYBOOT's own process. Static discovery is `ast`
   only; the runtime probe and the adapter contract runner are child interpreters with
   timeouts. This also keeps two SDKs with the same package name from colliding.
2. **Agent judgement** may be wrong or adversarial. It can only emit an `EmbodimentPlan`.
   Every string that reaches generated source is validated as a public identifier / dotted
   path (`plan.py`), numbers must be finite, and free text passes through `repr` or
   `safe_text`. A test feeds `"""\nimport os…` as `robot_family` and asserts nothing escapes.
3. **The evaluator's answers** are read only by `verification/`. The prompt builder refuses to
   send text containing evaluator markers, and the verifier scans the recorded prompt, the
   plan and all generated files for the evaluator's canary string.

## Stages

### 1. Discovery (`discovery/`)
`scanner.py` records modules, classes, dataclass fields, signatures, annotations, defaults,
docstrings (with Google-style `Args:` / `Attributes:` split per parameter/field), literal
constants with their PEP 257 attribute docstrings, and top-level vendor documents.

`probe_worker.py` then looks for a session class whose constructor defaults *prove* a
simulated transport (`sim://`, `mock://`, `replay://`, `loopback`). If none: no probing.
Otherwise it opens the link and walks handles breadth-first. `safety/policy.py` classifies
each method:

| class | rule |
|---|---|
| skip | async, needs arguments, or name holds an actuation/mutation token (deny beats allow) |
| lifecycle | single-token `open`/`connect`, `close`/`disconnect` |
| accessor | zero-arg, returns a non-dataclass SDK *handle* class |
| read | zero-arg **and** (read verb **or** docstring says read-only) |
| skip | everything else — "not provably read-only" |

Refusals are recorded with their reason; they are evidence too.

### 2. Agent (`agents/`)
`ClaudeAgentBackend` locates the CLI, runs `--help`, and builds the command from flags that
version really advertises (`--print`, `--output-format json`, `--tools ""`, `--safe-mode`,
`--no-session-persistence`, `--system-prompt`, `--json-schema`). The prompt goes over stdin;
the working directory is an empty temp dir; tools are disabled. Structural failures trigger a
repair retry carrying only the schema error; a CLI error drops `--json-schema` and retries.
Every attempt's prompt, argv and raw response are written to `agent/`.

`DeterministicAgentBackend` scores candidates with vocabulary profiles, requires that a
candidate's return record can fill *every* canonical field with a shape-compatible vendor
field, infers units from identifier suffixes then docs, solves argument→axis assignment over
parameter docs, and cross-checks units against probe samples (e.g. |accel|·scale ≈ 9.81).

### 3. Compiler (`compiler/`)
`validate_plan` + `check_plan_safety` decide which capabilities are *usable*; the rest are
compiled as methods that raise `CapabilityUnavailableError` with the reason. Motion is
withheld unless a trustworthy stop exists. Output is byte-for-byte deterministic.

### 4. Verification (`verification/`)
`native_runner.py` (child process) reads ground truth straight from the SDK through the
evaluator's symbols and conversions, wraps every public vendor method in a spy, then drives
the adapter. `contracts.py` judges the collected facts:

* **Capability discovery / Semantic mapping** — plan vs. snapshot, plan vs. evaluator.
* **Plan contracts** — signatures, access paths, argument coverage, units, evidence.
* **Native adapter contracts** — SI values equal SDK truth; physics sanity that needs no
  evaluator (gravity magnitude, unit quaternion, yaw range, byte count, monotonic stamps);
  velocity test vectors incl. clamping; stop forwards exactly the expected symbol.
* **dimOS package structure** — `tomllib` + `ast`: entry-point group/names/targets, typed
  streams, `@rpc` lifecycle chaining to `super()`, `@skill` rules, blueprint purity, and an
  allow-list of dimOS import paths confirmed against dimOS `main`.
* **Safety gates** — risk classes, stop is not a torque cut, `REAL_MOTION_ENABLED` literal,
  no env/file/process/eval escape hatches, **zero vendor calls from `velocity()`**, only
  zero-motion commands forwarded, motion cannot be enabled, dimOS modules never import the
  vendor SDK directly, nothing written outside `generated/`, no evaluator leakage.

Status is `VERIFIED` only if every check passes; `UNSCORED` if ground truth is missing.
