"""Build distributions and verify locked, non-editable installations outside Git.

Run with a new --output directory. No production model credentials are required.
Ticket 14 must rerun this on its final candidate after tickets 10 and 12.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(output, profiles, node):
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "AGENTLOOM_PROJECT_ROOT"):
        env.pop(key, None)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    env["SOURCE_DATE_EPOCH"] = subprocess.check_output(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, text=True).strip()
    logs = output / "logs"
    logs.mkdir()

    def command(label, argv, cwd=output, timeout=300):
        with (logs / (label + ".log")).open("w") as log:
            result = subprocess.run([str(arg) for arg in argv], cwd=cwd, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        assert result.returncode == 0, f"{label} failed; inspect {logs / (label + '.log')}"

    version = subprocess.check_output([str(node), "--version"], env=env, text=True).strip()
    assert version == "v22.19.0", f"Release verification pins Node 22.19.0, got {version}"
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    report = {"revision": revision, "dirty": dirty, "node": version,
              "lock_sha256": digest(ROOT / "uv.lock"), "profiles": {}}
    (output / "attempt.json").write_text(json.dumps(report, indent=2) + "\n")
    dist = output / "dist"
    # uv builds the wheel from the sdist by default: the wheel cannot rely on
    # an unshipped source resource. Build tools are fixed in pyproject.toml.
    command("build", ["uv", "build", "--out-dir", dist], cwd=ROOT)
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "agentloom/adapters/pi/bridge-v1.schema.json" in names
        for name in ("package.json", "package-lock.json", "tsconfig.json", "index.ts", "protocol.ts", "tools.ts", "model.ts"):
            assert "agentloom/adapters/pi/bridge/" + name in names, name
        assert len([name for name in names if "/tools/queries/" in name and name.endswith(".scm")]) == 56
        assert not any("/node_modules/" in name or "/dist/" in name or ".agentloom-install." in name for name in names)
        assert "agentloom/adapters/smolagents/prompts/toolcalling_agent.example.yaml" in names
    with tarfile.open(sdist) as archive:
        assert not any("/node_modules/" in name or "/bridge/dist/" in name or ".agentloom-install." in name for name in archive.getnames())
    report["artifacts"] = {path.name: digest(path) for path in (wheel, sdist)}
    probe = output / "profile_probe.py"
    shutil.copyfile(ROOT / "tests/packaging/profile_probe.py", probe)
    shutil.copyfile(ROOT / "tests/mcp_test/fixtures/stdio_server.py", output / "stdio_server.py")
    for profile in profiles:
        requirements = output / (profile + "-requirements.txt")
        command(profile + "-export", ["uv", "export", "--locked", "--extra", profile, "--no-dev", "--no-emit-project", "-o", requirements], cwd=ROOT)
        prefix = output / (profile + "-env")
        command(profile + "-venv", ["uv", "venv", "--python", "3.12", prefix])
        python = prefix / "bin/python"
        command(profile + "-dependencies", ["uv", "pip", "sync", "--python", python, "--require-hashes", requirements])
        command(profile + "-wheel", ["uv", "pip", "install", "--python", python, "--no-deps", wheel])
        command(profile + "-check", ["uv", "pip", "check", "--python", python])
        results = []

        def run_case(case):
            workspace = output / (profile + "-" + case)
            command(profile + "-" + case, [python, "-I", probe, "--profile", profile,
                                          "--case", case, "--workspace", workspace], timeout=180)
            proof = json.loads((workspace / "report.json").read_text())
            print(json.dumps({"profile": profile, "case": case, "status": proof["status"]}), flush=True)
            return proof

        if profile == "pi":
            results.append(run_case("missing_sdk"))
            command("pi-sdk-install", [python, "-I", "-m", "agentloom", "install-runtime", "pi"])
            command("pi-sdk-idempotent", [python, "-I", "-m", "agentloom", "install-runtime", "pi"])
            results.extend(run_case(case) for case in ("stale_bridge", "missing_asset"))
            cases = ("help", "missing_yaml", "missing_smol", "no_tools", "read", "outline_python",
                     "outline_typescript", "ast", "lsp", "mcp", "memory", "goal", "provider_failure", "child_failure")
        else:
            cases = ("help", "missing_yaml", "legacy", "outline_python", "mcp", "memory")
        with ThreadPoolExecutor(max_workers=3) as pool:
            results.extend(pool.map(run_case, cases))
        report["profiles"][profile] = {"requirements_sha256": digest(requirements), "cases": results}
    report["status"] = "passed"
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profiles", nargs="+", choices=["pi", "smol"], default=["pi", "smol"])
    parser.add_argument("--node", type=Path, default=Path(shutil.which("node") or "node"))
    args = parser.parse_args()
    validate(args.output.resolve(), args.profiles, args.node.resolve())
