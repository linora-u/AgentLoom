"""Public installer behavior with controlled external npm packages."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from agentloom.runtimes.pi.install import install_pi
from click.testing import CliRunner


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from agentloom.runtimes.pi import install

    source = Path(install.__file__).parent
    bridge = tmp_path / "pi/bridge"
    bridge.mkdir(parents=True)
    sources = [source / "bridge" / name for name in ("package.json", "package-lock.json", "tsconfig.json")]
    sources.extend((source / "bridge").glob("*.ts"))
    for path in sources:
        shutil.copyfile(path, bridge / path.name)
    for schema in source.glob("bridge-v*.schema.json"):
        shutil.copyfile(schema, bridge.parent / schema.name)
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "npm-calls.jsonl"
    tsc_log = tmp_path / "tsc-calls.jsonl"
    node = install.find_node(os.environ.copy())
    launcher = binary / "node"
    launcher.write_text(f"#!{sys.executable}\nimport os,sys\nos.execv({node!r}, [{node!r}, *sys.argv[1:]])\n")
    launcher.chmod(0o755)
    tsc_script = r"""
const fs = require('node:fs');
fs.appendFileSync(process.env.TEST_TSC_CALLS, JSON.stringify(process.argv.slice(2)) + '\n');
fs.mkdirSync('dist', {recursive: true});
const sources = fs.readdirSync('.').filter(path => path.endsWith('.ts'));
for (const source of sources) {
  fs.writeFileSync('dist/' + source.replace(/\.ts$/, '.js'), 'export const fixture=true;');
}
const sourceText = sources.map(source => fs.readFileSync(source, 'utf8')).join('\n');
const mismatch = sourceText.includes('TEST_HANDSHAKE_SDK_MISMATCH');
const missingNewline = sourceText.includes('TEST_HANDSHAKE_MISSING_NEWLINE');
const oversized = sourceText.includes('TEST_HANDSHAKE_OVERSIZED');
const sdkVersion = mismatch ? '0.0.0' : '0.87.1';
fs.writeFileSync('dist/index.js', `import {createInterface} from 'node:readline';
import {realpathSync} from 'node:fs';
const input=createInterface({input:process.stdin,crlfDelay:Infinity});
input.on('line',line=>{
 if(process.env.TEST_HANDSHAKE_FAIL) process.exit(4);
 const privateDir=realpathSync(process.argv[2]);
 if(realpathSync(process.cwd())!==privateDir ||
    realpathSync(process.env.HOME)!==privateDir ||
    realpathSync(process.env.XDG_CONFIG_HOME)!==privateDir ||
    realpathSync(process.env.TMPDIR)!==privateDir) process.exit(5);
 const frame=JSON.parse(line);
 const payload={method:'handshake',runtime_id:'pi',protocol_version:2,bridge_version:1,sdk_version:'${sdkVersion}',node_version:process.versions.node,native_tool_contract:1,capabilities:{structured_tools:true,parallel_tools:true,checkpoint_resume:true,subagents:true,goal:true,stop_hooks:true,structured_output:true}};
 const response=JSON.stringify({version:2,kind:'response',instance_id:frame.instance_id,run_id:null,request_id:frame.request_id,payload,error:null});
 process.stdout.write(response+(${oversized} ? ' '.repeat(8*1024*1024) : '')+(${missingNewline} ? '' : '\\n'));
});
`);
"""
    npm = binary / "npm"
    npm.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
root=Path.cwd()
with Path(os.environ['TEST_NPM_CALLS']).open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('TEST_NPM_FAIL'):
 print('PRIVATE-REGISTRY-CREDENTIAL');sys.exit(3)
sdk=root/'node_modules/@earendil-works/pi-coding-agent';sdk.mkdir(parents=True,exist_ok=True)
(sdk/'package.json').write_text(json.dumps({{'version':'0.87.1','type':'module','main':'index.js'}}))
(sdk/'index.js').write_text('export const fixture = true;')
ai=root/'node_modules/@earendil-works/pi-ai';ai.mkdir(parents=True,exist_ok=True)
(ai/'package.json').write_text(json.dumps({{'version':'0.87.1','type':'module','main':'index.js'}}))
tsc=root/'node_modules/typescript/bin/tsc';tsc.parent.mkdir(parents=True,exist_ok=True)
tsc.write_text({tsc_script!r})
''')
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TEST_NPM_CALLS", str(log))
    monkeypatch.setenv("TEST_TSC_CALLS", str(tsc_log))
    return bridge, tmp_path / "runtime", log, tsc_log


def source_bridge(installation):
    return installation[0]


def runtime_root(installation):
    return installation[1]


def npm_log(installation):
    return installation[2]


def tsc_log(installation):
    return installation[3]


def installed_bridge(installation):
    return runtime_root(installation) / "bridge"


def calls(installation):
    path = npm_log(installation)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def tsc_calls(installation):
    path = tsc_log(installation)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assert_installed_entry_handshakes(entry: Path) -> None:
    request = {
        "version": 2,
        "kind": "request",
        "instance_id": "installed-runtime-probe",
        "run_id": None,
        "request_id": "host:installed-runtime-probe",
        "payload": {
            "method": "handshake",
            "protocol_version": 2,
            "bridge_version": 1,
            "native_tool_contract": 1,
        },
    }
    with tempfile.TemporaryDirectory() as private_dir:
        env = os.environ.copy()
        env.update(
            HOME=private_dir,
            XDG_CONFIG_HOME=private_dir,
            TMPDIR=private_dir,
        )
        result = subprocess.run(
            ["node", str(entry), private_dir],
            cwd=private_dir,
            env=env,
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    response = json.loads(result.stdout)
    assert response["payload"]["runtime_id"] == "pi"
    assert response["payload"]["sdk_version"] == "0.87.1"


@pytest.mark.parametrize("changed_file", ["index.ts", "tools.ts", "model.ts", "nested/helper.ts"])
def test_install_downloads_lock_builds_once_and_rebuilds_changed_source(installation, changed_file):
    entry = install_pi(source_bridge(installation), runtime_root(installation))
    assert entry.is_file()
    assert not (source_bridge(installation) / "node_modules").exists()
    assert not (source_bridge(installation) / "dist").exists()
    assert calls(installation) == [["ci", "--ignore-scripts", "--include=dev", "--no-audit", "--no-fund"]]
    assert len(tsc_calls(installation)) == 1
    assert install_pi(source_bridge(installation), runtime_root(installation)) == entry
    assert len(calls(installation)) == 1
    assert len(tsc_calls(installation)) == 1
    changed = source_bridge(installation) / changed_file
    changed.parent.mkdir(exist_ok=True)
    with changed.open("a") as stream:
        stream.write("\n// changed bridge source\n")
    assert install_pi(source_bridge(installation), runtime_root(installation)) == entry
    assert len(calls(installation)) == 2
    assert len(tsc_calls(installation)) == 2


@pytest.mark.parametrize("package", ["@earendil-works/pi-coding-agent", "@earendil-works/pi-ai"])
def test_version_mismatch_fails_before_download(installation, package):
    manifest = source_bridge(installation) / "package.json"
    data = json.loads(manifest.read_text())
    data["dependencies"][package] = "0.0.1"
    manifest.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="version.*lock"):
        install_pi(source_bridge(installation), runtime_root(installation))
    assert not calls(installation)


@pytest.mark.parametrize("source", ["model.ts", "tools.ts"])
def test_installer_rebuilds_when_a_tool_or_model_bridge_changes(installation, source):
    install_pi(source_bridge(installation), runtime_root(installation))
    with (source_bridge(installation) / source).open("a") as stream:
        stream.write("\n// changed installed bridge behavior\n")
    install_pi(source_bridge(installation), runtime_root(installation))
    assert len(calls(installation)) == 2


def test_installer_repairs_a_missing_compiled_tool_module(installation):
    install_pi(source_bridge(installation), runtime_root(installation))
    (installed_bridge(installation) / "dist/tools.js").unlink()
    install_pi(source_bridge(installation), runtime_root(installation))
    assert (installed_bridge(installation) / "dist/tools.js").is_file()
    assert len(calls(installation)) == 2


def test_installer_tracks_a_renamed_protocol_schema(installation):
    install_pi(source_bridge(installation), runtime_root(installation))
    schema = next(source_bridge(installation).parent.glob("bridge-v*.schema.json"))
    version = int(schema.name.split("-v")[1].split(".")[0])
    schema.rename(schema.with_name(f"bridge-v{version + 1}.schema.json"))
    assert install_pi(source_bridge(installation), runtime_root(installation)).is_file()
    assert len(calls(installation)) == 2


def test_missing_protocol_schema_fails_before_download(installation):
    for schema in source_bridge(installation).parent.glob("bridge-v*.schema.json"):
        schema.unlink()
    with pytest.raises(RuntimeError, match="schema"):
        install_pi(source_bridge(installation), runtime_root(installation))
    assert not calls(installation)


def test_failed_install_can_retry_without_a_false_ready_marker(installation, monkeypatch):
    monkeypatch.setenv("TEST_NPM_FAIL", "1")
    with pytest.raises(RuntimeError) as caught:
        install_pi(source_bridge(installation), runtime_root(installation))
    assert "PRIVATE-REGISTRY-CREDENTIAL" not in str(caught.value)
    assert not (runtime_root(installation) / "ready.json").exists()
    monkeypatch.delenv("TEST_NPM_FAIL")
    assert install_pi(source_bridge(installation), runtime_root(installation)).is_file()
    assert len(calls(installation)) == 2


def test_failed_bridge_handshake_never_publishes_ready_runtime(
    installation,
    monkeypatch,
):
    monkeypatch.setenv("TEST_HANDSHAKE_FAIL", "1")

    with pytest.raises(RuntimeError, match="handshake"):
        install_pi(source_bridge(installation), runtime_root(installation))

    assert not runtime_root(installation).exists()
    monkeypatch.delenv("TEST_HANDSHAKE_FAIL")
    assert install_pi(source_bridge(installation), runtime_root(installation)).is_file()
    assert len(calls(installation)) == 2


@pytest.mark.parametrize(
    ("failure_marker", "error"),
    [
        ("TEST_HANDSHAKE_SDK_MISMATCH", "handshake mismatch"),
        ("TEST_HANDSHAKE_MISSING_NEWLINE", "handshake process failed"),
        ("TEST_HANDSHAKE_OVERSIZED", "exceeds 8 MiB"),
    ],
)
def test_invalid_staged_handshake_preserves_the_ready_runtime(
    installation,
    failure_marker,
    error,
):
    old_entry = install_pi(source_bridge(installation), runtime_root(installation))
    ready = runtime_root(installation) / "ready.json"
    old_ready = ready.read_bytes()
    old_entry_contents = old_entry.read_bytes()
    source = source_bridge(installation) / "index.ts"
    with source.open("a") as stream:
        stream.write(f"\n// {failure_marker}\n")

    with pytest.raises(RuntimeError, match=error):
        install_pi(source_bridge(installation), runtime_root(installation))

    assert ready.read_bytes() == old_ready
    assert old_entry.read_bytes() == old_entry_contents
    assert_installed_entry_handshakes(old_entry)
    assert not list(runtime_root(installation).parent.glob(".pi-staging-*"))

    source.write_text(
        source.read_text().replace(f"// {failure_marker}", "// corrected bridge handshake")
    )
    assert install_pi(source_bridge(installation), runtime_root(installation)) == old_entry
    assert len(calls(installation)) == 3
    assert len(tsc_calls(installation)) == 3
    assert not list(runtime_root(installation).parent.glob(".pi-old-*"))
    assert_installed_entry_handshakes(old_entry)


def test_concurrent_processes_share_one_installation(installation):
    command = [sys.executable, "-c", "from pathlib import Path; from agentloom.runtimes.pi.install import install_pi; import sys; install_pi(Path(sys.argv[1]), Path(sys.argv[2]))", str(source_bridge(installation)), str(runtime_root(installation))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True, timeout=30), range(2)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert len(calls(installation)) == 1


def test_install_runtime_command_is_discoverable_and_rejects_unknown_runtime():
    from agentloom.__main__ import main

    runner = CliRunner()
    assert "runtime" in runner.invoke(main, ["--help"]).output
    assert main.commands["runtime"].callback.__module__ == "agentloom.runtimes.pi.cli"
    assert "install" in runner.invoke(main, ["runtime", "--help"]).output
    assert runner.invoke(main, ["runtime", "install", "unknown"]).exit_code == 2
