# Demo script

## 60 seconds

| t | Do | Say |
|---|---|---|
| 0:00 | `ls generated/` → only `.gitkeep`. Open `fixtures/mystery_go2_sdk/mysterybot/sport.py`. | "This is a vendor SDK BODYBOOT has never seen. There is no adapter for it. Note `joystick(v1, v2, v3)` — nothing says vx/vy/yaw." |
| 0:10 | `uv run bodyboot demo --agent claude` | "Step 1 scans the SDK and probes it — read-only. Look: 7 calls *refused* by the safety policy. It never touched `joystick`, `freeze` or `go_limp`." |
| 0:20 | (Claude is thinking, ~60 s; talk over it) | "Claude gets three things: mapping instructions, the snapshot, an output schema. No tools, empty directory, and never the answer key. It decides *semantics only*." |
| 0:45 | Plan lines appear with warnings | "It found that the vendor order is strafe-advance-turn, that turn is clockwise degrees, and it rejected `go_limp` as a stop because the robot would collapse." |
| 0:50 | Steps 3–5 | "A deterministic compiler — no model — wrote a native adapter and an external dimOS plugin. Then 71 independent contracts executed against the SDK. Real motion: disabled, proven by spies." |
| 0:58 | `.\scripts\demo.ps1 -Open` or open `report.html` | "Evidence for every mapping, and the full audit trail of what Claude was sent." |

If the network is unavailable, `--agent deterministic` runs the identical pipeline in ~3 s.

## Proving it is not staged

```bash
# The exact prompt and raw response of the run you just watched
ls .bodyboot/runs/<run-id>/agent/

# The agent never saw the answers
grep -c "EVALUATOR-CANARY" .bodyboot/runs/<run-id>/agent/prompt.txt      # 0

# Break the plan the way a careless engineer would, recompile, re-verify -> FAILED
#   edit embodiment_plan.json: set the imu accel scale to 1.0
uv run bodyboot compile .bodyboot/runs/<run-id>/embodiment_plan.json
uv run bodyboot verify <run-id>

# Rename every symbol in the SDK and run again
uv run pytest tests/test_verifier_and_cli.py -k renamed -q
```

## Try the generated adapter by hand

```bash
uv run python scripts/try_adapter.py            # newest run, or pass a run id
```

It imports the freshly generated `bodyboot_native_adapter`, connects to the fixture SDK and
prints SI-unit samples. `velocity(0.3, 0.1, 0.5)` prints the would-be vendor call
(`v1=0.1, v2=0.3, v3=-28.65`) with `forwarded_to_vendor=False` - nothing is sent.
