from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentloom.app.harnessing.isolation import IsolationUnavailable, preflight_isolation
from agentloom.app.harnessing.process import execute_isolated
from agentloom.app.harnessing.snapshot import derive_candidate, load_bundle, snapshot_application


@dataclass
class Context:
    root: Path
    candidate_id: str
    experiment_id: str = "experiment"
    trial_id: str = "trial"
    task_snapshot_id: str = "task"


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    app = project / "applications" / "sample"
    (project / "config").mkdir(parents=True)
    (project / "config" / "system.yaml").write_text("{}\n")
    (app / "prompts").mkdir(parents=True)
    (app / "prompts" / "main.md").write_text("frozen instructions")
    (app / "skills" / "nested" / "references").mkdir(parents=True)
    (app / "skills" / "nested" / "SKILL.md").write_text("---\nname: sample\ndescription: sample\n---\nRead references/detail.txt")
    (app / "skills" / "nested" / "references" / "detail.txt").write_text("frozen reference")
    (app / "operations.py").write_text('''
import errno, os, subprocess, sys
from pathlib import Path

def probe(payload, bundle, context):
    root = Path(context['root'])
    candidate = Path(bundle['root']) / 'project' / 'applications/sample/prompts/main.md'
    results = {'prompt': candidate.read_text(), 'parent_secret': os.environ.get('HARNESS_CANARY')}
    for name, operation in (
        ('read', lambda: Path(payload['hidden']).read_text()),
        ('metadata', lambda: os.chmod(payload['hidden'], 0o777)),
        ('write_bundle', lambda: candidate.write_text('corrupt')),
        ('forge_receipt', lambda: (root / 'process/input.json').write_text('corrupt')),
        ('read_env', lambda: Path('/proc/self/environ').read_bytes()),
    ):
        try:
            operation()
            results[name] = 'UNEXPECTED_ALLOW'
        except PermissionError:
            results[name] = 'denied'
    (root / 'workspace/link').symlink_to(payload['hidden'])
    try:
        (root / 'workspace/link').read_text()
        results['symlink'] = 'UNEXPECTED_ALLOW'
    except PermissionError:
        results['symlink'] = 'denied'
    child = subprocess.run(['/usr/bin/cat', payload['hidden']], capture_output=True)
    results['exec'] = child.returncode != 0
    (root / 'output/result.txt').write_text('delivered')
    from agentloom.config.config import load_project_config
    config = load_project_config(Path(bundle['root']) / 'project')
    results['model_bound'] = config.llm.models['test'].api_key == 'private-canary'
    environment = subprocess.run(['/usr/bin/env'], capture_output=True, text=True).stdout
    results['child_secret'] = 'private-canary' in environment
    return results
''')
    return project, app


def test_snapshot_preserves_all_skill_bytes_and_portable_identity(project, tmp_path):
    root, app = project
    (app / "config").mkdir()
    (app / "config/model.yaml").write_text("model:\n  test:\n    api_key: private-canary\n")
    hidden = app / "hidden.json"
    hidden.write_text('{"score":99}')
    bundle = snapshot_application(root, app, tmp_path / "bundles", excluded_paths=[hidden])
    assert "private-canary" not in (bundle.application_root / "config/model.yaml").read_text()
    assert not (bundle.application_root / "hidden.json").exists()
    (app / "skills/nested/references/detail.txt").write_text("live mutation")
    assert (bundle.application_root / "skills/nested/references/detail.txt").read_text() == "frozen reference"
    moved = tmp_path / "moved"
    shutil.copytree(bundle.root, moved)
    assert load_bundle(moved).candidate_id == bundle.candidate_id
    (moved / "project/applications/sample/prompts/main.md").chmod(0o644)
    (moved / "project/applications/sample/prompts/main.md").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        load_bundle(moved)


def test_candidate_cannot_change_fixed_business_code(project, tmp_path):
    root, app = project
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    with pytest.raises(ValueError, match="fixed code"):
        derive_candidate(bundle, {"applications/sample/operations.py": "print('changed')"}, tmp_path / "bundles", editable=["tools"])
    derived = derive_candidate(bundle, {"applications/sample/prompts/main.md": "improved"}, tmp_path / "bundles", editable=["system_prompt"], validate=lambda project, app: None)
    assert derived.candidate_id != bundle.candidate_id
    assert (bundle.application_root / "prompts/main.md").read_text() == "frozen instructions"


@pytest.mark.skipif(sys.platform != "linux", reason="Real Linux kernel isolation acceptance; Darwin backend is rejected")
def test_public_managed_process_denies_hidden_exec_metadata_symlink_and_credentials(project, tmp_path, monkeypatch):
    try:
        preflight_isolation()
    except IsolationUnavailable as error:
        pytest.skip(str(error))
    root, app = project
    hidden = tmp_path / "hidden-score"
    hidden.write_text("held out score")
    original_mode = hidden.stat().st_mode
    monkeypatch.setenv("HARNESS_CANARY", "parent-only")
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    context = Context(tmp_path / "trial", bundle.candidate_id)
    models = {"model": {"test": {"model": "test", "adapter": "openai_chat", "api_key": "private-canary"}}}
    result = execute_isolated(bundle, context, "applications.sample.operations:probe", {"hidden": str(hidden)}, denied_paths=[hidden], model_config=models, timeout=20)
    assert result == {"prompt": "frozen instructions", "parent_secret": None, "read": "denied", "metadata": "denied", "write_bundle": "denied", "forge_receipt": "denied", "read_env": "denied", "symlink": "denied", "exec": True, "model_bound": True, "child_secret": False}
    assert (context.root / "output/result.txt").read_text() == "delivered"
    assert hidden.stat().st_mode == original_mode
    assert execute_isolated(bundle, context, "applications.sample.operations:probe", {"hidden": str(hidden)}, denied_paths=[hidden], model_config=models) == result
    assert len(list((context.root / "process/attempts").iterdir())) == 1
    assert "private-canary" not in (context.root / "process/input.json").read_text()
    assert json.loads((context.root / "process/attempts/000001/cleanup.json").read_text())["physical_exit_confirmed"]
