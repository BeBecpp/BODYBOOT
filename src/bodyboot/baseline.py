"""Capability-category comparison against the human-written Go2 baseline.

The human side is a hand-curated inventory of *what kinds of things*
BeBecpp/DimOs_Windows implements (read-only inspection of commit 90c8370,
2026-09-20). No code, constants-as-code or structure was taken from it, and it
played no part in generation - it is only read after verification.
"""

from __future__ import annotations

from pathlib import Path

from bodyboot.plan import load_plan
from bodyboot.runs import RunPaths

BASELINE_REPO = "https://github.com/BeBecpp/DimOs_Windows"
BASELINE_COMMIT = "90c8370"

# (category, human implementation summary, canonical capability or None)
_CATEGORIES: tuple[tuple[str, str, str | None], ...] = (
    ("Connection / transport",
     "WebRTC session via unitree_webrtc_connect + aiortc; AES-128 key handling; typed connect errors",
     "__connection__"),
    ("RGB input", "rgb24 video track decoded into timestamped camera frames; JPEG/MJPEG over HTTP",
     "camera.rgb"),
    ("IMU telemetry", "not implemented (no IMU subscription found)", "imu"),
    ("Odometry / pose telemetry", "not implemented (pose source reported as unavailable)", "odometry"),
    ("Velocity control", "(vx, vy, wz) in m/s, m/s, rad/s via sport Move; hard caps, single-owner velocity mux",
     "locomotion.velocity"),
    ("Stop", "StopMove publish; latched emergency stop; zero on disconnect/owner change/shutdown",
     "locomotion.stop"),
    ("Health / heartbeat", "health endpoints; heartbeat watchdog with e-stop callback", "connection.health"),
    ("Watchdogs / deadman", "camera-stale, manual-TTL, connection and target-lost watchdogs; keyboard deadman",
     None),
    ("Mode management", "motion-mode query/select, BalanceStand arming, avoids ReleaseMode", None),
    ("Perception / following", "target locker (Kalman + IoU + re-id), face tracking, follow control law", None),
    ("Agent / tool surface", "FastAPI command API with bearer auth; dimOS skill container with 4 @skill methods",
     "__skills__"),
)  # fmt: skip


def render_baseline_comparison(paths: RunPaths) -> str:
    plan = load_plan(paths.plan)
    manifest = paths.read_manifest()
    rows = []
    for category, human, canonical in _CATEGORIES:
        if canonical == "__connection__":
            generated = f"`{plan.connection.session_symbol}` open/close lifecycle (simulator transport only)"
        elif canonical == "__skills__":
            generated = ("generated dimOS skill container: robot_health, describe_embodiment, stop_robot, "
                         "move_velocity (mock-only); `dimos.blueprints` entry points")  # fmt: skip
        elif canonical is None:
            generated = "— not generated (out of scope for this milestone)"
        else:
            cap = plan.get(canonical)
            generated = (
                f"`{canonical}` → `{cap.vendor_symbol}` ({cap.risk}, confidence {cap.confidence:.2f})"
                if cap
                else f"`{canonical}` — NOT mapped in this run"
            )
            if canonical == "locomotion.velocity" and cap:
                generated += " — **mock-only, never forwarded**"
        rows.append(f"| {category} | {human} | {generated} |")

    return f"""# Human baseline vs. BODYBOOT-generated interface

Run `{paths.run_id}` · agent `{manifest.get("agent", "?")}` · verification
`{manifest.get("verification_status", "not run")}`.

> A human previously integrated a Go2 manually.
> BODYBOOT independently generated an interface from a vendor SDK surface.

Comparison is by **capability category only**. The human repository
({BASELINE_REPO} @ `{BASELINE_COMMIT}`) was inspected read-only, *after* generation and
verification; no implementation code was copied or transformed.

| Capability category | Human: DimOs_Windows (real Go2, WebRTC) | BODYBOOT: generated from `{plan.sdk_package}` SDK |
|---|---|---|
{chr(10).join(rows)}

## Reading this honestly

* **Different robots, different evidence.** The human work drives a *real* Unitree Go2 and
  was exercised at AdventureX 2026. BODYBOOT's output was verified only against a
  deterministic fictional SDK fixture. Nothing here was run on hardware, and no claim of
  hardware validation is made.
* **Scale of effort.** The human adapter/connection/control core is roughly 3,400 lines of
  Python (plus ~1,000 lines of perception and ~5,000 lines of tests across the repo). The
  BODYBOOT interface was compiled in seconds from an agent-inferred plan of a few hundred
  lines of JSON.
* **What the human has that BODYBOOT does not:** hard-won operational safety (watchdogs,
  deadman, velocity ownership, mode arming quirks, firmware workarounds) and perception.
  Those are lessons from contact with a real robot and are future work here.
* **What BODYBOOT produced that the human baseline lacks:** IMU and odometry interfaces with
  explicit unit/frame conversions, machine-checkable evidence for every mapping, and an
  independent contract suite.
* The generated motion path is **mock-only**; the human path really moves a robot.
"""


def write_baseline_comparison(paths: RunPaths) -> Path:
    paths.baseline.write_text(render_baseline_comparison(paths), encoding="utf-8")
    return paths.baseline
