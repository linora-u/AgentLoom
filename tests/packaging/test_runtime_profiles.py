"""Distribution metadata is the installer's public dependency contract."""
from __future__ import annotations

from email.parser import BytesParser
from pathlib import Path
import subprocess
from zipfile import ZipFile

from packaging.requirements import Requirement


def test_built_wheel_selects_smol_only_through_its_explicit_profile(tmp_path):
    root = Path(__file__).resolve().parents[2]
    constraints = tmp_path / "build-constraints.txt"
    exported = subprocess.run(
        ["uv", "export", "--locked", "--only-group", "build", "--no-emit-project", "-o", str(constraints)],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    built = subprocess.run(
        ["uv", "build", "--wheel", str(root), "--out-dir", str(tmp_path),
         "--build-constraints", str(constraints), "--require-hashes"],
        capture_output=True, text=True, timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        names_in_wheel = wheel.namelist()
        assert not any("/node_modules/" in name or "/bridge/dist/" in name
                       or ".agentloom-install." in name for name in names_in_wheel)
        assert "agentloom/adapters/pi/bridge/tools.ts" in names_in_wheel
        assert "agentloom/adapters/pi/bridge/model.ts" in names_in_wheel
        schemas = list((root / "src/adapters/pi").glob("bridge-v*.schema.json"))
        assert schemas
        assert all("agentloom/adapters/pi/" + path.name in names_in_wheel for path in schemas)
        metadata = BytesParser().parsebytes(wheel.read(next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )))
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist") or []]
    def names(extra):
        return {r.name for r in requirements if r.marker is None or r.marker.evaluate({"extra": extra})}
    assert "smolagents" not in names("pi")
    assert "openinference-instrumentation-smolagents" not in names("pi")
    assert {"smolagents", "openinference-instrumentation-smolagents"} <= names("smol")
    professional = {"serena-agent", "ast-grep-cli", "grep-ast", "tree-sitter-language-pack",
                    "go-bin", "nodejs-bin", "libclang", "tree-sitter-c", "networkx"}
    assert not professional & names("pi")
    assert professional <= names("code")
    assert {"tree-sitter", "tree-sitter-bash"} <= names("pi")  # Shared Shell governance.
    assert {"pi", "smol", "code"} <= set(metadata.get_all("Provides-Extra") or [])
