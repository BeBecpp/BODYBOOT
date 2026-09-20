"""Discovery: static AST scanning and the policy-gated read-only runtime probe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bodyboot.discovery.models import CallableInfo, ParamInfo, SdkSnapshot
from bodyboot.discovery.scanner import ScanError, discover, parse_doc_section, scan_sdk
from bodyboot.safety.policy import ProbeClass, classify_probe_call


def test_scanner_finds_modules_classes_and_public_symbols(snapshot: SdkSnapshot) -> None:
    names = {m.name for m in snapshot.modules}
    assert {"mysterybot", "mysterybot.client", "mysterybot.camera", "mysterybot.state",
            "mysterybot.sport"} <= names  # fmt: skip
    assert snapshot.packages == ["mysterybot"]
    assert snapshot.stats.classes >= 7 and snapshot.stats.public_symbols >= 23
    assert snapshot.find_class("mysterybot.state.ImuPacket").is_dataclass  # type: ignore[union-attr]


def test_scanner_captures_signatures_annotations_and_param_docs(snapshot: SdkSnapshot) -> None:
    method = snapshot.find_callable("mysterybot.sport.SportChannel.joystick")
    assert method is not None
    assert method.signature() == "joystick(v1: float, v2: float, v3: float) -> StickCommand"
    docs = {p.name: p.doc for p in method.params}
    assert "CLOCKWISE" in (docs["v3"] or "") and "LEFT" in (docs["v1"] or "")
    init = snapshot.find_class("mysterybot.client.RobotSession").init  # type: ignore[union-attr]
    assert init is not None and [p.default for p in init.params] == ["DEFAULT_ENDPOINT", "2.0"]


def test_scanner_captures_field_docs_and_constants(snapshot: SdkSnapshot) -> None:
    imu = snapshot.find_class("mysterybot.state.ImuPacket")
    assert imu is not None
    assert "standard" in (imu.field("accel_g").doc or "")  # type: ignore[union-attr]
    assert imu.field("quat_wxyz").annotation == "tuple[float, float, float, float]"  # type: ignore[union-attr]
    constants = {c.name: c for c in snapshot.all_constants()}
    assert constants["MAX_TURN_DPS"].value == 100.0
    assert "degrees per second" in (constants["MAX_TURN_DPS"].doc or "")
    assert snapshot.documents and "Conventions" in snapshot.documents[0].text


def test_scanner_never_imports_the_sdk(tmp_path: Path) -> None:
    package = tmp_path / "boom"
    package.mkdir()
    (package / "__init__.py").write_text(
        'raise SystemExit("imported!")\n\nclass Arm:\n    def read_joint(self) -> float:\n        """Angle."""\n',
        encoding="utf-8",
    )
    snapshot = scan_sdk(tmp_path)
    assert snapshot.find_callable("boom.Arm.read_joint") is not None


def test_snapshot_describes_but_does_not_map(snapshot: SdkSnapshot) -> None:
    """The discovery stage must not smuggle canonical answers into the snapshot."""
    text = snapshot.model_dump_json()
    for canonical in ("camera.rgb", "locomotion.velocity", "locomotion.stop", "connection.health", "canonical"):
        assert canonical not in text


def test_scan_rejects_paths_without_python(tmp_path: Path) -> None:
    with pytest.raises(ScanError):
        scan_sdk(tmp_path)
    with pytest.raises(ScanError):
        scan_sdk(tmp_path / "missing")


def test_google_docstring_sections_are_parsed() -> None:
    doc = (
        "Do it.\n\nArgs:\n    speed: Forward speed\n        in m/s.\n"
        "    turn (float): Turn rate.\n\nReturns:\n    Nothing."
    )
    assert parse_doc_section(doc, ("Args",)) == {"speed": "Forward speed in m/s.", "turn": "Turn rate."}


def test_runtime_probe_reads_sensors_with_samples(snapshot: SdkSnapshot) -> None:
    assert snapshot.runtime.executed and "sim://" in (snapshot.runtime.transport_proof or "")
    observed = {o.symbol: o for o in snapshot.runtime.observed()}
    imu = observed["mysterybot.state.StateChannel.read_imu_packet"]
    assert imu.policy_class == ProbeClass.READ and len(imu.samples) == 2
    assert imu.samples[0]["accel_g"] == [0.0, -0.02, 0.9996]
    frame = observed["mysterybot.camera.VideoPipe.fetch_rgb"].samples[0]
    assert frame["buf"]["__bytes__"] == 160 * 90 * 3


def test_runtime_probe_never_calls_actuation(snapshot: SdkSnapshot) -> None:
    status = {o.symbol.rsplit(".", 1)[-1]: o.status for o in snapshot.runtime.observations}
    for dangerous in ("joystick", "freeze", "go_limp", "posture", "set_exposure"):
        assert status[dangerous] == "skipped", dangerous
    assert snapshot.stats.runtime_skipped >= 5


def test_runtime_probe_refuses_non_simulated_transport(tmp_path: Path) -> None:
    package = tmp_path / "realbot"
    package.mkdir()
    (package / "__init__.py").write_text(
        "import pathlib\n\n"
        "class Link:\n"
        '    def __init__(self, endpoint: str = "udp://192.168.123.161:8082") -> None:\n'
        "        pathlib.Path(__file__).with_name('CONSTRUCTED').write_text('x')\n"
        "    def open(self) -> None: ...\n"
        "    def read_state(self) -> int:\n        return 1\n",
        encoding="utf-8",
    )
    snapshot = discover(tmp_path)
    assert snapshot.runtime.executed is False
    assert "refused to probe" in snapshot.runtime.reason
    assert not (package / "CONSTRUCTED").exists(), "the session must not even be constructed"


def test_runtime_probe_survives_a_broken_sdk(tmp_path: Path) -> None:
    package = tmp_path / "brokenbot"
    package.mkdir()
    (package / "__init__.py").write_text(
        'class Link:\n    def __init__(self, endpoint: str = "sim://x") -> None:\n'
        '        raise RuntimeError("driver missing")\n',
        encoding="utf-8",
    )
    snapshot = discover(tmp_path)
    assert snapshot.runtime.executed is False and "driver missing" in snapshot.runtime.reason
    assert snapshot.stats.classes == 1  # the static description is still delivered


@pytest.mark.parametrize(
    ("name", "params", "returns", "doc", "expected"),
    [
        ("read_state", [], "State", None, ProbeClass.READ),
        ("stand_up", [], "None", None, ProbeClass.SKIP),
        ("getVelocityCommand", [], "Cmd", "Read-only.", ProbeClass.SKIP),  # deny beats allow
        ("fetch_frame", [ParamInfo(name="index")], "Frame", None, ProbeClass.SKIP),
        ("lights", [], "LightChannel", None, ProbeClass.ACCESSOR),
        ("snapshot", [], "Report", None, ProbeClass.SKIP),  # returns data, no read evidence
        ("keepalive", [], "Report", "Pure query; no side effects.", ProbeClass.READ),
        ("open", [], "None", None, ProbeClass.OPEN),
    ],
)
def test_probe_policy_classification(
    name: str, params: list[ParamInfo], returns: str, doc: str | None, expected: ProbeClass
) -> None:
    method = CallableInfo(name=name, qualname=f"x.Y.{name}", params=params, returns=returns, docstring=doc)
    assert classify_probe_call(method, {"LightChannel"}).probe_class is expected


def test_snapshot_is_json_roundtrippable(snapshot: SdkSnapshot) -> None:
    again = SdkSnapshot.model_validate(json.loads(snapshot.model_dump_json()))
    assert again.stats == snapshot.stats
