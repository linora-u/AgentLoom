"""Deterministic tests for release campaign capsule attestation."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.memory_feature_validation.scripts import (  # noqa: E402
    memory_review_campaign_capsule as capsule_module,
)
from applications.memory_feature_validation.scripts.memory_review_campaign_capsule import (  # noqa: E402
    CAMPAIGN_LLM_CONFIG_FD_ENV,
    CAPSULE_ACTIVE_ENV,
    CAPSULE_ROOT_ENV,
    _canonical_bytes_identity,
    _checkout_state,
    _clean_python_env,
    _detach_hardlinked_files,
    _git_metadata_is_isolated,
    _git_metadata_paths,
    _materialize_commit_tree,
    _raw_tree_entries,
    _regular_files_unshared,
    _resolved_inside,
    _secure_cleanup,
    _tree_manifest_hash,
    _unsafe_git_customization_issues,
    active_capsule_bootstrap_issues,
    canonical_json_hash,
    capsule_descriptor_issues,
    guarded_runtime_command,
    trusted_control_plane_matches,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _new_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "capsule@example.invalid")
    _git(path, "config", "user.name", "Capsule Test")
    return path


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _valid_descriptor() -> dict[str, object]:
    descriptor: dict[str, object] = {
        "schema_version": 1,
        "git_commit": "a" * 40,
        "source_manifest_hash": "b" * 64,
        "dataset_manifest_hash": "c" * 64,
        "model_contract_hash": "d" * 64,
        "uv_lock_hash": "e" * 64,
        "uv_version": "uv 0.11.15",
        "uv_binary_hash": "0" * 64,
        "git_version": "git version 2.50.0",
        "git_binary_hash": "6" * 64,
        "lock_sync_ok": True,
        "python_version": "3.12",
        "python_cache_tag": "cpython-312",
        "python_binary_hash": "f" * 64,
        "loom_hash": "1" * 64,
        "loom_shebang_target_hash": "2" * 64,
        "distribution_set_hash": "3" * 64,
        "venv_manifest_hash": "5" * 64,
        "stdlib_manifest_hash": "7" * 64,
        "checkout_manifest_hash": "8" * 64,
        "runtime_env_contract_hash": "9" * 64,
        "write_guard_binary_hash": "a" * 64,
        "bootstrap_valid": True,
        "checkout_exact": True,
        "write_guard_available": True,
        "python_is_capsule": True,
        "python_prefix_is_capsule": True,
        "loom_is_capsule": True,
        "loom_shebang_is_capsule": True,
        "loom_shebang_matches_python": True,
        "src_origin_is_capsule": True,
        "runner_origin_is_capsule": True,
        "src_origin_relative": "src/__init__.py",
        "runner_origin_relative": (
            "applications/memory_feature_validation/scripts/"
            "run_memory_review_campaign.py"
        ),
        "user_site_disabled": True,
        "bytecode_writes_disabled": True,
        "capsule_tree_read_only": True,
        "capsule_files_unshared": True,
        "git_metadata_write_guarded": True,
        "model_config_memory_only": True,
        "private_parent": True,
    }
    descriptor["capsule_id"] = canonical_json_hash(descriptor)
    return descriptor


def _reseal(descriptor: dict[str, object]) -> dict[str, object]:
    result = dict(descriptor)
    result.pop("capsule_id", None)
    result["capsule_id"] = canonical_json_hash(result)
    return result


def test_complete_capsule_descriptor_is_accepted() -> None:
    assert capsule_descriptor_issues(_valid_descriptor()) == []


def test_main_venv_or_wrong_import_origin_is_rejected() -> None:
    descriptor = _valid_descriptor()
    descriptor["python_is_capsule"] = False
    descriptor["src_origin_relative"] = "/mutable/main/src/__init__.py"

    assert capsule_descriptor_issues(_reseal(descriptor)) == [
        "capsule runtime escaped its isolated locked environment",
        "capsule src import origin was invalid",
    ]


def test_capsule_id_detects_descriptor_drift() -> None:
    descriptor = _valid_descriptor()
    descriptor["distribution_set_hash"] = "4" * 64

    assert capsule_descriptor_issues(descriptor) == [
        "capsule id did not match its descriptor"
    ]


def test_capsule_lock_and_runner_origin_are_required() -> None:
    descriptor = _valid_descriptor()
    descriptor["lock_sync_ok"] = False
    descriptor["runner_origin_relative"] = "tmp/copied_runner.py"

    assert capsule_descriptor_issues(_reseal(descriptor)) == [
        "capsule runtime escaped its isolated locked environment",
        "capsule runner origin was invalid",
    ]


def test_resolved_origin_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "capsule"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    escaped = root / "runner.py"
    escaped.symlink_to(outside)

    assert _resolved_inside(escaped, root) is False


def test_venv_manifest_detects_same_version_code_replacement(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    package = venv / "site-packages" / "package.py"
    package.parent.mkdir(parents=True)
    package.write_text("VERSION = '1.0'\nVALUE = 1\n", encoding="utf-8")
    before = _tree_manifest_hash(venv)
    package.write_text("VERSION = '1.0'\nVALUE = 2\n", encoding="utf-8")

    assert _tree_manifest_hash(venv) != before


def test_venv_manifest_is_path_free_without_sentinel_collisions(
    tmp_path: Path,
) -> None:
    hashes: list[str] = []
    for name in ("capsule-a", "capsule-with-a-longer-name"):
        root = tmp_path / name
        venv = root / ".venv"
        script = venv / "bin" / "loom"
        script.parent.mkdir(parents=True)
        script.write_text(
            f"#!{root}/.venv/bin/python\nroot={root}\nliteral=$CAPSULE_ROOT\n",
            encoding="utf-8",
        )
        (venv / "loom-link").symlink_to(script)
        hashes.append(_tree_manifest_hash(venv, canonical_root=root))

    assert hashes[0] == hashes[1]
    actual_root = tmp_path / "real-root"
    assert _canonical_bytes_identity(b"$CAPSULE_ROOT", actual_root) != (
        _canonical_bytes_identity(str(actual_root).encode(), actual_root)
    )


def test_cleanup_removes_private_config_when_git_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    temporary = tmp_path / "private"
    capsule = temporary / "checkout"
    config = capsule / "config" / "llm.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("api_key: secret\n", encoding="utf-8")

    def fail_git(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(capsule_module.subprocess, "run", fail_git)

    _secure_cleanup(REPO_ROOT, temporary, capsule)

    assert not temporary.exists()


def test_provision_rejects_aliased_metadata_before_any_git_or_uv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    metadata = repo / ".git"
    metadata.mkdir(parents=True)
    aliased = metadata / "aliased"
    aliased.write_text("metadata", encoding="utf-8")
    os.link(aliased, tmp_path / "external-alias")
    monkeypatch.setattr(
        capsule_module,
        "_trusted_git",
        lambda: pytest.fail("Git ran before metadata isolation"),
    )
    monkeypatch.setattr(
        capsule_module,
        "_trusted_uv",
        lambda: pytest.fail("uv ran before metadata isolation"),
    )
    monkeypatch.setattr(
        capsule_module,
        "_unsafe_git_customization_issues",
        lambda _root: pytest.fail(
            "customization inspection ran before metadata isolation"
        ),
    )

    with pytest.raises(RuntimeError, match="Git metadata is externally aliased"):
        with capsule_module.provision_capsule(
            repo,
            expected_commit="a" * 40,
        ):
            pytest.fail("aliased metadata reached capsule provisioning")


def test_public_environment_flags_cannot_mark_main_checkout_as_capsule(
    monkeypatch,
) -> None:
    monkeypatch.setenv(CAPSULE_ACTIVE_ENV, "1")
    monkeypatch.setenv(CAPSULE_ROOT_ENV, str(REPO_ROOT))

    issues = active_capsule_bootstrap_issues(REPO_ROOT)

    assert "capsule private bootstrap token was invalid" in issues
    assert "capsule repository was not a linked worktree" in issues


def test_capsule_environment_is_an_allowlist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/forged-git")
    monkeypatch.setenv("LD_AUDIT", "/tmp/evil.so")
    monkeypatch.setenv("SHELL", "/tmp/evil-shell")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example")
    uv = tmp_path / "trusted" / "uv"
    uv.parent.mkdir()
    uv.touch()

    env = _clean_python_env(tmp_path / "capsule", token="a" * 64, uv=uv)

    assert "GIT_DIR" not in env
    assert "LD_AUDIT" not in env
    assert "SHELL" not in env
    assert CAMPAIGN_LLM_CONFIG_FD_ENV not in env
    assert "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_SECRET" not in env
    assert env["HTTPS_PROXY"] == "https://proxy.example"
    assert env["PATH"].split(":")[0].endswith("capsule/.venv/bin")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS release guard")
def test_runtime_guard_denies_writes_to_capsule(tmp_path: Path) -> None:
    protected = tmp_path.resolve() / "capsule"
    protected.mkdir()
    target = protected / "changed.txt"
    command = guarded_runtime_command(
        ["/bin/sh", "-c", f"printf changed > '{target}'"],
        repo_root=protected,
    )

    completed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert completed.returncode != 0
    assert not target.exists()


def test_raw_materialization_ignores_replace_refs_and_detects_tampering(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path / "repo")
    payload = repo / "payload.txt"
    payload.write_text("safe\n", encoding="utf-8")
    safe_commit = _commit_all(repo, "safe")
    payload.write_text("evil\n", encoding="utf-8")
    evil_commit = _commit_all(repo, "evil")
    _git(repo, "reset", "--hard", safe_commit)
    _git(repo, "replace", safe_commit, evil_commit)

    assert _git(repo, "show", f"{safe_commit}:payload.txt").stdout == "evil\n"
    assert "Git replace refs are not allowed" in _unsafe_git_customization_issues(
        repo
    )
    checkout = tmp_path / "checkout"
    _git(
        repo,
        "--no-replace-objects",
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(checkout),
        safe_commit,
    )
    _materialize_commit_tree(checkout, safe_commit)

    assert (checkout / "payload.txt").read_text(encoding="utf-8") == "safe\n"
    assert _checkout_state(checkout, safe_commit)[0] is True
    (checkout / "payload.txt").write_text("evil\n", encoding="utf-8")
    assert _checkout_state(checkout, safe_commit)[0] is False


def test_historical_commit_cannot_supply_an_older_runner(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path / "repo")
    trusted_files = (
        "applications/memory_feature_validation/scripts/run_memory_review_campaign.py",
        "src/__init__.py",
        "config/system.yaml",
        "pyproject.toml",
        "uv.lock",
    )
    for relative in trusted_files:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"trusted:{relative}\n", encoding="utf-8")
    commit = _commit_all(repo, "trusted control plane")

    assert trusted_control_plane_matches(repo, commit) is True
    runner = (
        repo
        / "applications"
        / "memory_feature_validation"
        / "scripts"
        / "run_memory_review_campaign.py"
    )
    runner.write_text("historical runner drift\n", encoding="utf-8")
    assert trusted_control_plane_matches(repo, commit) is False

    _git(repo, "checkout", "--", runner.relative_to(repo).as_posix())
    shadow = runner.parent / "yaml.py"
    shadow.write_text("raise RuntimeError('shadow executed')\n", encoding="utf-8")
    shadow_commit = _commit_all(repo, "historical import shadow")
    shadow.unlink()
    assert trusted_control_plane_matches(repo, shadow_commit) is False

    for relative in (
        "applications/__init__.py",
        "applications/memory_feature_validation.py",
    ):
        parent_shadow = repo / relative
        parent_shadow.parent.mkdir(parents=True, exist_ok=True)
        parent_shadow.write_text(
            "raise RuntimeError('parent shadow executed')\n",
            encoding="utf-8",
        )
        parent_shadow_commit = _commit_all(repo, f"shadow with {relative}")
        parent_shadow.unlink()
        assert trusted_control_plane_matches(repo, parent_shadow_commit) is False


def test_raw_materialization_never_runs_configured_clean_filter(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path / "repo")
    (repo / "payload.txt").write_text("safe\n", encoding="utf-8")
    commit = _commit_all(repo, "safe")
    side_effect = tmp_path / "filter-ran"
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("*.txt filter=attack\n", encoding="utf-8")
    _git(repo, "config", "filter.attack.clean", f"touch {side_effect}")
    _git(repo, "config", "filter.attack.required", "true")

    assert "custom Git content filters are not allowed" in (
        _unsafe_git_customization_issues(repo)
    )
    checkout = tmp_path / "checkout"
    _git(
        repo,
        "--no-replace-objects",
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(checkout),
        commit,
    )
    _materialize_commit_tree(checkout, commit)

    assert (checkout / "payload.txt").read_text(encoding="utf-8") == "safe\n"
    assert not side_effect.exists()
    assert _checkout_state(checkout, commit)[0] is True


def test_raw_materialization_rejects_gitlinks_and_escaping_symlinks(
    tmp_path: Path,
) -> None:
    symlink_repo = _new_repo(tmp_path / "symlink-repo")
    (symlink_repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    os.symlink("../outside", symlink_repo / "escape")
    symlink_commit = _commit_all(symlink_repo, "escaping symlink")
    symlink_checkout = tmp_path / "symlink-checkout"
    _git(
        symlink_repo,
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(symlink_checkout),
        symlink_commit,
    )
    with pytest.raises(RuntimeError, match="escaping symlink"):
        _materialize_commit_tree(symlink_checkout, symlink_commit)

    gitlink_repo = _new_repo(tmp_path / "gitlink-repo")
    (gitlink_repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    object_commit = _commit_all(gitlink_repo, "object")
    _git(
        gitlink_repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{object_commit},nested",
    )
    _git(gitlink_repo, "commit", "-q", "-m", "gitlink")
    gitlink_commit = _git(gitlink_repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(RuntimeError, match="unsupported tree entry"):
        _raw_tree_entries(gitlink_repo, gitlink_commit)


def test_preexisting_hardlink_is_detected_and_detached(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    target = capsule / "payload.py"
    target.write_text("safe\n", encoding="utf-8")
    external = tmp_path / "external-link"
    os.link(target, external)

    assert _regular_files_unshared(capsule) is False
    _detach_hardlinked_files(capsule)
    assert _regular_files_unshared(capsule) is True

    external.write_text("evil\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "safe\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS release guard")
def test_runtime_guard_denies_new_hardlinks_and_metadata_changes(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "capsule"
    protected.mkdir()
    target = protected / "target"
    target.write_text("safe", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    link = external / "link"

    link_result = subprocess.run(
        guarded_runtime_command(
            ["/bin/ln", str(target), str(link)],
            repo_root=protected,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    chmod_result = subprocess.run(
        guarded_runtime_command(
            ["/bin/chmod", "777", str(external)],
            repo_root=protected,
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert link_result.returncode != 0
    assert chmod_result.returncode != 0
    assert not link.exists()
    assert target.read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS release guard")
def test_runtime_guard_protects_linked_git_metadata_but_allows_outputs(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path / "repo")
    (repo / "payload.txt").write_text("safe\n", encoding="utf-8")
    commit = _commit_all(repo, "safe")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), commit)
    metadata_paths = _git_metadata_paths(linked)

    for index, metadata_root in enumerate(metadata_paths):
        probe = metadata_root / f"capsule-guard-{index}"
        result = subprocess.run(
            guarded_runtime_command(
                ["/bin/sh", "-c", f"printf changed > '{probe}'"],
                repo_root=linked,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not probe.exists()

    external = tmp_path / "campaign-output"
    result = subprocess.run(
        guarded_runtime_command(
            ["/bin/sh", "-c", f"printf allowed > '{external}'"],
            repo_root=linked,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert external.read_text(encoding="utf-8") == "allowed"

    common = metadata_paths[-1]
    alias_target = common / "capsule-alias-target"
    alias_target.write_text("metadata", encoding="utf-8")
    alias = tmp_path / "metadata-hardlink"
    os.link(alias_target, alias)
    with pytest.raises(RuntimeError, match="externally aliased"):
        guarded_runtime_command(["/usr/bin/true"], repo_root=linked)
    alias.unlink()
    alias_target.unlink()

    external_target = tmp_path / "external-target"
    external_target.write_text("outside", encoding="utf-8")
    metadata_symlink = common / "capsule-external-symlink"
    metadata_symlink.symlink_to(external_target)
    with pytest.raises(RuntimeError, match="externally aliased"):
        guarded_runtime_command(["/usr/bin/true"], repo_root=linked)
    metadata_symlink.unlink()

    alternates = common / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(str(tmp_path / "external-objects"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="externally aliased"):
        guarded_runtime_command(["/usr/bin/true"], repo_root=linked)
    alternates.unlink()

    internal_alternates = common / "internal-alternates"
    internal_alternates.write_text(
        str(tmp_path / "external-objects"),
        encoding="utf-8",
    )
    alternates.symlink_to(internal_alternates)
    with pytest.raises(RuntimeError, match="externally aliased"):
        guarded_runtime_command(["/usr/bin/true"], repo_root=linked)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket contract")
def test_git_metadata_allows_unaliased_socket_but_rejects_fifo() -> None:
    with tempfile.TemporaryDirectory(prefix="al-gm-", dir="/tmp") as raw_root:
        metadata_root = Path(raw_root)
        fsmonitor_socket = metadata_root / "s"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(fsmonitor_socket))
            assert _git_metadata_is_isolated([metadata_root]) is True
        fsmonitor_socket.unlink()

        fifo = metadata_root / "f"
        os.mkfifo(fifo)
        assert _git_metadata_is_isolated([metadata_root]) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS release guard")
def test_runtime_guard_blocks_unix_socket_proxy_writes(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    target = protected / "target"
    target.write_text("safe", encoding="utf-8")
    connected = threading.Event()

    with tempfile.TemporaryDirectory(prefix="al-sock-", dir="/tmp") as raw_root:
        socket_path = Path(raw_root) / "s"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(socket_path))
            server.listen(1)
            server.settimeout(0.5)

            def proxy_daemon() -> None:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    return
                with connection:
                    connection.recv(1024)
                    connected.set()
                    target.write_text("changed-by-proxy", encoding="utf-8")

            daemon = threading.Thread(target=proxy_daemon)
            daemon.start()
            child = subprocess.run(
                guarded_runtime_command(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import socket,sys;"
                            "s=socket.socket(socket.AF_UNIX);"
                            "s.connect(sys.argv[1]);s.sendall(b'write')"
                        ),
                        str(socket_path),
                    ],
                    repo_root=protected,
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            daemon.join(timeout=2)

    assert child.returncode != 0
    assert not daemon.is_alive()
    assert connected.is_set() is False
    assert target.read_text(encoding="utf-8") == "safe"
