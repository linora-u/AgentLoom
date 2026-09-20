"""Public installer behavior with controlled external npm packages."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from click.testing import CliRunner
import pytest

from agentloom.adapters.pi.install import install_pi


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from agentloom.adapters.pi import install

    source = Path(install.__file__).parent
    bridge = tmp_path / "pi/bridge"
    bridge.mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "tsconfig.json", "index.ts", "protocol.ts"):
        shutil.copyfile(source / "bridge" / name, bridge / name)
    shutil.copyfile(source / "bridge-v2.schema.json", bridge.parent / "bridge-v2.schema.json")
    binary = tmp_path / "bin"
    binary.mkdir()
    node = install.find_node(os.environ.copy())
    launcher = binary / "node"
    launcher.write_text(f"#!{sys.executable}\nimport os,sys\nos.execv({node!r}, [{node!r}, *sys.argv[1:]])\n")
    launcher.chmod(0o755)
    npm = binary / "npm"
    npm.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
root=Path.cwd()
with (root/'npm-calls.jsonl').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('TEST_NPM_FAIL'):
 print('PRIVATE-REGISTRY-CREDENTIAL');sys.exit(3)
sdk=root/'node_modules/@earendil-works/pi-coding-agent';sdk.mkdir(parents=True,exist_ok=True)
(sdk/'package.json').write_text(json.dumps({{'version':'0.79.4','type':'module','main':'index.js'}}))
(sdk/'index.js').write_text('export const fixture = true;')
tsc=root/'node_modules/typescript/bin/tsc';tsc.parent.mkdir(parents=True,exist_ok=True)
tsc.write_text("const fs=require('node:fs');fs.mkdirSync('dist',{{recursive:true}});fs.writeFileSync('dist/index.js','export const fixture=true;');fs.writeFileSync('dist/protocol.js','export const fixture=true;');")
''')
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    return bridge


def calls(bridge):
    path = bridge / "npm-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("changed_file", ["index.ts", "tools.ts", "model.ts", "nested/helper.ts"])
def test_install_downloads_lock_builds_once_and_rebuilds_changed_source(installation, changed_file):
    entry = install_pi(installation)
    assert entry.is_file()
    assert calls(installation) == [["ci", "--ignore-scripts", "--include=dev", "--no-audit", "--no-fund"]]
    assert install_pi(installation) == entry
    assert len(calls(installation)) == 1
    changed = installation / changed_file
    changed.parent.mkdir(exist_ok=True)
    with changed.open("a") as stream:
        stream.write("\n// changed bridge source\n")
    assert install_pi(installation) == entry
    assert len(calls(installation)) == 2


def test_version_mismatch_fails_before_download(installation):
    manifest = installation / "package.json"
    data = json.loads(manifest.read_text())
    data["dependencies"]["@earendil-works/pi-coding-agent"] = "0.0.1"
    manifest.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="version.*lock"):
        install_pi(installation)
    assert not calls(installation)


def test_failed_install_can_retry_without_a_false_ready_marker(installation, monkeypatch):
    monkeypatch.setenv("TEST_NPM_FAIL", "1")
    with pytest.raises(RuntimeError) as caught:
        install_pi(installation)
    assert "PRIVATE-REGISTRY-CREDENTIAL" not in str(caught.value)
    assert not (installation / ".agentloom-install.json").exists()
    monkeypatch.delenv("TEST_NPM_FAIL")
    assert install_pi(installation).is_file()
    assert len(calls(installation)) == 2


def test_concurrent_processes_share_one_installation(installation):
    command = [sys.executable, "-c", "from pathlib import Path; from agentloom.adapters.pi.install import install_pi; import sys; install_pi(Path(sys.argv[1]))", str(installation)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True, timeout=30), range(2)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert len(calls(installation)) == 1


def test_install_runtime_command_is_discoverable_and_rejects_unknown_runtime():
    from agentloom.__main__ import main

    runner = CliRunner()
    assert "install-runtime" in runner.invoke(main, ["--help"]).output
    assert runner.invoke(main, ["install-runtime", "unknown"]).exit_code == 2
