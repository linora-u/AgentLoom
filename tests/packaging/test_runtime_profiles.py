"""Distribution metadata is the installer's public dependency contract."""
from __future__ import annotations

from email.parser import BytesParser
from pathlib import Path
import subprocess
from zipfile import ZipFile

from packaging.requirements import Requirement


def test_built_wheel_selects_smol_only_through_its_explicit_profile(tmp_path):
    root = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ["uv", "build", "--wheel", str(root), "--out-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        names_in_wheel = wheel.namelist()
        assert not any("/node_modules/" in name or "/bridge/dist/" in name
                       or ".agentloom-install." in name for name in names_in_wheel)
        assert "agentloom/adapters/pi/bridge/tools.ts" in names_in_wheel
        assert "agentloom/adapters/pi/bridge/model.ts" in names_in_wheel
        assert "agentloom/adapters/pi/bridge-v1.schema.json" in names_in_wheel
        metadata = BytesParser().parsebytes(wheel.read(next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )))
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist") or []]
    def names(extra):
        return {r.name for r in requirements if r.marker is None or r.marker.evaluate({"extra": extra})}
    assert "smolagents" not in names("pi")
    assert "openinference-instrumentation-smolagents" not in names("pi")
    assert {"smolagents", "openinference-instrumentation-smolagents"} <= names("smol")
    assert {"pi", "smol"} <= set(metadata.get_all("Provides-Extra") or [])
