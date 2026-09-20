# MysteryBot M2 SDK (Python binding) 0.9.3

Vendor SDK for the MysteryBot **M2**, a 12-DoF quadruped in the same class as
other small research/consumer robot dogs.

> This archive ships the **simulator transport only** (`sim://`). It opens no
> sockets and needs no robot. All channels return deterministic data.

## Quick start

```python
from mysterybot import RobotSession

link = RobotSession()  # defaults to sim://loopback
link.open()

print(link.heartbeat())  # link supervision
print(link.state().read_packet())
frame = link.video().fetch_rgb()

link.close()
```

## Layout

| Module              | Purpose                                  |
|---------------------|------------------------------------------|
| `mysterybot.client` | session / transport, link supervision    |
| `mysterybot.camera` | head camera                              |
| `mysterybot.state`  | telemetry (body state, inertial, battery)|
| `mysterybot.sport`  | locomotion commands                      |

## Conventions - read this

* Telemetry (`mysterybot.state`) uses ISO 8855: x forward, y left, z up,
  counter-clockwise positive. Timestamps are microseconds since `open()`.
* Angles in telemetry are in **degrees**; inertial rates are in **deg/s**;
  accelerations are in **g**.
* Quaternions are scalar-first `(w, x, y, z)`.
* The locomotion command API (`mysterybot.sport`) keeps the axis convention of
  the handheld remote for backwards compatibility. It is **not** the same as
  the telemetry convention. See `SportChannel.joystick`.

## Safety

`SportChannel.freeze()` is the normal stop. `SportChannel.go_limp()` removes
joint torque and the robot falls over - never use it as a routine stop.
