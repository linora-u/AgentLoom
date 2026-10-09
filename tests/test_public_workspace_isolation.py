"""Private workspace configuration never enters public trees or commit history."""
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".github/scripts/public_sync.py"
spec = importlib.util.spec_from_file_location("workspace_sync", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
PRIVATE = ("applications/private_app",)
PROJECT = '[project]\nname = "framework"\nversion = "1.0"\n'
LOCK = '''version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "framework"
version = "1.0"
source = { editable = "." }
dependencies = [{ name = "shared" }]

[[package]]
name = "shared"
version = "1.0"
source = { registry = "https://example.test/simple" }
'''
PRIVATE_PACKAGE = '''
[[package]]
name = "private-app"
version = "0.1"
source = { editable = "applications/private_app" }
dependencies = [{ name = "private-only-library" }, { name = "shared" }]

[[package]]
name = "private-only-library"
version = "2.0"
source = { registry = "https://example.test/simple" }
'''


def workspace_lock():
    return LOCK.replace('[[package]]', '[manifest]\nmembers = ["framework", "private-app"]\n\n[[package]]', 1) + PRIVATE_PACKAGE


def test_private_only_workspace_preserves_framework_configuration():
    private = PROJECT + '\n[tool.uv.workspace]\nmembers = ["./applications/private_app"]\n'
    assert sync.public_project_blob(private.encode(), PRIVATE) == PROJECT.encode()
    public = sync.public_lock_blob(workspace_lock().encode(), PRIVATE)
    assert tomllib.loads(public.decode()) == tomllib.loads(LOCK)


def test_mixed_workspaces_keep_public_members_and_their_locked_packages():
    project = PROJECT + '\n[tool.uv.workspace]\nmembers = ["applications/private_app", "applications/demo"]\n'
    parsed = tomllib.loads(sync.public_project_blob(project.encode(), PRIVATE).decode())
    assert parsed["tool"]["uv"]["workspace"]["members"] == ["applications/demo"]
    lock = workspace_lock().replace('"framework", "private-app"', '"framework", "private-app", "demo"')
    lock += '\n[[package]]\nname = "demo"\nversion = "1.0"\nsource = { editable = "applications/demo" }\n'
    parsed = tomllib.loads(sync.public_lock_blob(lock.encode(), PRIVATE).decode())
    assert {p["name"] for p in parsed["package"]} == {"framework", "shared", "demo"}
    assert parsed["manifest"]["members"] == ["framework", "demo"]


def test_private_dependency_references_are_removed_from_public_lock_metadata():
    lock = workspace_lock().replace('dependencies = [{ name = "shared" }]',
                                   'dependencies = [{ name = "shared" }, { name = "private-app" }]')
    lock = lock.replace('\n[[package]]\nname = "shared"',
                        '\n[package.metadata]\nrequires-dist = [{ name = "shared" }, { name = "private-app" }]\n\n[[package]]\nname = "shared"')
    public = sync.public_lock_blob(lock.encode(), PRIVATE)
    assert b"private-app" not in public and b"private-only-library" not in public
    assert tomllib.loads(public.decode())["package"][0]["dependencies"] == [{"name": "shared"}]


def test_lock_fingerprint_retains_real_versions_and_conditions():
    conditional = LOCK.replace('{ name = "shared" }', '{ name = "shared", marker = "sys_platform == \'win32\'" }')
    conditional += '\ndependencies = [{ name = "leaf", marker = "sys_platform == \'win32\'" }]\n'
    # Drop only the child's marker; the root's condition still protects it.
    implied = conditional.rsplit('dependencies = ', 1)[0] + 'dependencies = [{ name = "leaf" }]\n'
    assert sync.lock_fingerprint(conditional.encode()) == sync.lock_fingerprint(implied.encode())
    assert sync.lock_fingerprint(LOCK.encode()) != sync.lock_fingerprint(LOCK.replace('version = "1.0"', 'version = "2.0"').encode())
    assert sync.lock_fingerprint(conditional.encode()) != sync.lock_fingerprint(
        conditional.replace("sys_platform == 'win32'", "sys_platform == 'linux'", 1).encode())


def test_extra_markers_are_not_collapsed_across_package_contexts():
    lock = LOCK.replace('{ name = "shared" }', '{ name = "shared", marker = "extra == \'optional\'" }')
    lock += '\ndependencies = [{ name = "leaf", marker = "extra == \'optional\'" }]\n'
    changed = lock.rsplit('dependencies = ', 1)[0] + 'dependencies = [{ name = "leaf" }]\n'
    assert sync.lock_fingerprint(lock.encode()) != sync.lock_fingerprint(changed.encode())


def test_private_dev_dependency_metadata_is_filtered():
    package = {"name": "framework", "dependencies": [{"name": "shared"}],
               "dev-dependencies": {"dev": [{"name": "private-app"}, {"name": "shared"}]},
               "metadata": {"requires-dev": {"dev": [{"name": "private-app"}, {"name": "shared"}]}}}
    filtered = sync.public_package_record(package, {"private-app"})
    assert filtered["dev-dependencies"]["dev"] == [{"name": "shared"}]
    assert filtered["metadata"]["requires-dev"]["dev"] == [{"name": "shared"}]
    assert package["dev-dependencies"]["dev"][0]["name"] == "private-app"


def commit(repo, message):
    sync.command("git", "add", "-A", cwd=repo)
    sync.command("git", "commit", "-m", message, cwd=repo)
    return sync.command("git", "rev-parse", "HEAD", cwd=repo).decode().strip()


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    source, public = tmp_path / "source", tmp_path / "public"
    for repo in (source, public):
        repo.mkdir()
        sync.command("git", "init", "-b", "main", cwd=repo)
        sync.command("git", "config", "user.name", "Fixture", cwd=repo)
        sync.command("git", "config", "user.email", "fixture@example.test", cwd=repo)
        (repo / ".github").mkdir()
        (repo / sync.CONFIG_PATH).write_text(json.dumps({"private_prefixes": list(PRIVATE)}))
        (repo / "pyproject.toml").write_text(PROJECT)
        (repo / "uv.lock").write_text(LOCK)
        commit(repo, "Framework baseline")
    base = sync.command("git", "rev-parse", "HEAD", cwd=source).decode().strip()
    monkeypatch.chdir(source)
    return source, public, base


def add_private_workspace(source):
    (source / "pyproject.toml").write_text(PROJECT + '\n[tool.uv.workspace]\nmembers = ["applications/private_app"]\n')
    (source / "uv.lock").write_text(workspace_lock())
    return commit(source, "Private application environment")


def test_private_workspace_commits_collapse_and_no_public_pr_is_required(repositories):
    source, _, base = repositories
    add_private_workspace(source)
    assert not sync.has_public_changes(sync.public_history(base, "HEAD", PRIVATE))
    desired = sync.public_tree(sync.local_tree("HEAD"), PRIVATE)
    assert desired == sync.local_tree(base)


def test_export_hides_intermediate_workspace_configuration_and_preserves_public_updates(repositories, tmp_path):
    source, public, base = repositories
    private_commit = add_private_workspace(source)
    (source / "uv.lock").write_text(workspace_lock().replace('name = "shared"\nversion = "1.0"',
                                                         'name = "shared"\nversion = "2.0"'))
    commit(source, "Update framework dependency")
    target = tmp_path / "export"
    sync.command("git", "clone", "--no-checkout", str(public), str(target))
    history = sync.public_history(base, "HEAD", PRIVATE)
    mapping, _ = sync.export_public_history(history, target, PRIVATE)
    assert mapping[0]["source"] == private_commit and not mapping[0]["exported"]
    assert mapping[1]["exported"]
    for row in mapping:
        for path in ("pyproject.toml", "uv.lock"):
            blob = sync.command("git", "show", f"{row['public']}:{path}", cwd=target)
            assert b"private_app" not in blob and b"private-app" not in blob
            assert b"private-only-library" not in blob
    lock = tomllib.loads(sync.command("git", "show", "HEAD:uv.lock", cwd=target).decode())
    assert next(p["version"] for p in lock["package"] if p["name"] == "shared") == "2.0"


def test_guard_rejects_private_metadata_in_history_even_after_revert(repositories):
    source, _, _ = repositories
    add_private_workspace(source)
    (source / "pyproject.toml").write_text(PROJECT)
    (source / "uv.lock").write_text(LOCK)
    commit(source, "Restore current framework configuration")
    result = subprocess.run([sys.executable, str(SCRIPT.with_name("check_public_tree.py"))],
                            cwd=source, capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "private uv workspace" in result.stderr


def test_guard_accepts_filtered_public_history(repositories, tmp_path):
    source, public, base = repositories
    add_private_workspace(source)
    (source / "public.txt").write_text("Framework change\n")
    commit(source, "Public framework update")
    target = tmp_path / "export"
    sync.command("git", "clone", "--no-checkout", str(public), str(target))
    sync.export_public_history(sync.public_history(base, "HEAD", PRIVATE), target, PRIVATE)
    result = subprocess.run([sys.executable, str(SCRIPT.with_name("check_public_tree.py"))],
                            cwd=target, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
