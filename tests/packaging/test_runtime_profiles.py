"""Distribution metadata is the installer's public dependency contract."""
from __future__ import annotations

import json
import runpy
import shutil
import subprocess
import sys
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement


def test_missing_pi_asset_targets_the_installed_runtime(tmp_path, monkeypatch):
    prefix = tmp_path / "isolated-env"
    monkeypatch.setattr(sys, "prefix", str(prefix))
    probe = runpy.run_path(str(Path(__file__).with_name("profile_probe.py")))

    assert probe["_pi_damage_target"]("missing_asset") == (
        prefix.resolve() / "share/pi/bridge/dist/tools.js"
    )


@pytest.mark.parametrize("rename_schema", [False, True], ids=["current-schema", "next-schema-filename"])
def test_built_wheel_selects_smol_only_through_its_explicit_profile(tmp_path, rename_schema):
    root = Path(__file__).resolve().parents[2]
    constraints = tmp_path / "build-constraints.txt"
    exported = subprocess.run(
        ["uv", "export", "--locked", "--only-group", "build", "--no-emit-project", "-o", str(constraints)],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    source = root
    if rename_schema:
        source = tmp_path / "future-package"
        shutil.copytree(root / "src", source / "src", ignore=shutil.ignore_patterns(
            "node_modules", "dist", "__pycache__", "*.pyc", ".agentloom-install.*"))
        for name in ("pyproject.toml", "README.md"):
            shutil.copyfile(root / name, source / name)
        vendored = Path("third_party/harness2")
        shutil.copytree(root / vendored / "harness2", source / vendored / "harness2",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("LICENSE", "PROVENANCE.md"):
            shutil.copyfile(root / vendored / name, source / vendored / name)
        schema = next((source / "src/runtimes/pi").glob("bridge-v*.schema.json"))
        version = int(schema.name.split("-v")[1].split(".")[0])
        schema.rename(schema.with_name(f"bridge-v{version + 1}.schema.json"))
    built = subprocess.run(
        ["uv", "build", "--wheel", str(source), "--out-dir", str(tmp_path),
         "--build-constraints", str(constraints), "--require-hashes"],
        capture_output=True, text=True, timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        names_in_wheel = wheel.namelist()
        assert not any("/node_modules/" in name or "/bridge/dist/" in name
                       or ".agentloom-install." in name for name in names_in_wheel)
        assert "agentloom/runtimes/pi/bridge/tools.ts" in names_in_wheel
        assert "agentloom/runtimes/pi/bridge/model.ts" in names_in_wheel
        assert "agentloom/runtimes/smolagents/prompts/toolcalling_agent.example.yaml" in names_in_wheel
        assert {f"harness2/core/{name}.py" for name in (
            "schedule", "recursion", "context", "proposer", "selection", "library",
        )} <= set(names_in_wheel)
        vendored_package = source / "third_party/harness2/harness2"
        for name in ("LICENSE", "UPSTREAM.json", "PATCHES.md"):
            assert wheel.read(f"harness2/{name}") == (vendored_package / name).read_bytes()
        assert wheel.read("harness2/LICENSE") == (source / "third_party/harness2/LICENSE").read_bytes()
        provenance = json.loads(wheel.read("harness2/UPSTREAM.json"))
        assert provenance["repository"] == "https://github.com/google-research/harness2"
        assert provenance["revision"] == "1cfe254e8d25b15f37048c3730134686abe60335"
        assert provenance["license"] == "Apache-2.0"
        schemas = list((source / "src/runtimes/pi").glob("bridge-v*.schema.json"))
        assert schemas
        assert all("agentloom/runtimes/pi/" + path.name in names_in_wheel for path in schemas)
        metadata = BytesParser().parsebytes(wheel.read(next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )))
    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist") or []]
    def names(extra):
        return {r.name for r in requirements if r.marker is None or r.marker.evaluate({"extra": extra})}
    assert "smolagents" not in names("pi")
    assert "openinference-instrumentation-smolagents" not in names("pi")
    assert {"smolagents", "openinference-instrumentation-smolagents"} <= names("smol")
    professional = {"grep-ast", "tree-sitter-language-pack", "networkx"}
    assert not professional & names("pi")
    assert professional <= names("code")
    assert {"tree-sitter", "tree-sitter-bash"} <= names("pi")  # Shared Shell governance.
    assert {"pi", "smol", "code"} <= set(metadata.get_all("Provides-Extra") or [])
