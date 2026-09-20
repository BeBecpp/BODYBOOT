"""Drive the most recently generated native adapter by hand against the mystery SDK fixture.

    uv run python scripts/try_adapter.py [run-id]

Everything printed comes from the generated code. velocity() is mock-only: it shows the
vendor call that WOULD be made and sends nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    generated = sorted(ROOT.glob("generated/native_*"))
    if len(sys.argv) > 1:
        generated = [ROOT / "generated" / f"native_{sys.argv[1]}"]
    if not generated or not generated[-1].is_dir():
        print("no generated adapter found - run `uv run bodyboot demo --agent deterministic` first")
        return 1
    sys.path[:0] = [str(ROOT / "fixtures" / "mystery_go2_sdk"), str(generated[-1])]

    from bodyboot_native_adapter import GeneratedRobotAdapter  # type: ignore[import-not-found]

    print(f"adapter: {generated[-1].name}\n")
    robot = GeneratedRobotAdapter()
    robot.connect()
    print("health   ", robot.health())
    print("imu      ", robot.imu())
    print("odometry ", robot.odometry())
    frame = robot.camera_rgb()
    print(f"camera    {frame.width}x{frame.height} {frame.encoding}, {len(frame.data)} bytes")
    print("velocity ", robot.velocity(0.3, 0.1, 0.5), " <- MOCK: nothing was sent")
    robot.stop()
    robot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
