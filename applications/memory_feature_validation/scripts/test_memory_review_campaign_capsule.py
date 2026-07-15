"""Deterministic tests for release campaign capsule attestation."""

from __future__ import annotations

import os
import py_compile
import socket
import stat
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
        "python_dont_write_bytecode": True,
        "pycache_prefix_exact": True,
        "pycache_prefix_inside_capsule": True,
        "pycache_prefix_empty": True,
        "pycache_prefix_read_only": True,
        "pycache_prefix_not_symlink": True,
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


@pytest.mark.parametrize(
    "field",
    [
        "python_dont_write_bytecode",
        "pycache_prefix_exact",
        "pycache_prefix_inside_capsule",
        "pycache_prefix_empty",
        "pycache_prefix_read_only",
        "pycache_prefix_not_symlink",
    ],
)
def test_capsule_descriptor_rejects_invalid_pycache_isolation(field: str) -> None:
    descriptor = _valid_descriptor()
    descriptor[field] = False

    assert capsule_descriptor_issues(_reseal(descriptor)) == [
        "capsule runtime escaped its isolated locked environment"
    ]


def test_pycache_contract_detects_missing_dirty_linked_writable_and_external_prefixes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "capsule"
    (root / ".venv").mkdir(parents=True)
    prefix = root / ".venv" / ".agentloom-pycache"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    monkeypatch.setattr(sys, "_xoptions", {"pycache_prefix": str(prefix)})
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(prefix))

    missing = capsule_module._pycache_prefix_contract(root)
    assert missing["pycache_prefix_empty"] is False
    assert missing["pycache_prefix_not_symlink"] is False

    prefix.mkdir(mode=0o700)
    writable = capsule_module._pycache_prefix_contract(root)
    assert writable["pycache_prefix_read_only"] is False

    (prefix / "derived.pyc").write_bytes(b"dirty")
    prefix.chmod(0o500)
    dirty = capsule_module._pycache_prefix_contract(root)
    assert dirty["pycache_prefix_empty"] is False
    prefix.chmod(0o700)
    (prefix / "derived.pyc").unlink()
    prefix.chmod(0o500)
    valid = capsule_module._pycache_prefix_contract(root)
    assert all(valid.values())
    wrong = str(root / ".venv" / "wrong-cache")
    for mismatched_layer in ("runtime", "xoption", "environment"):
        monkeypatch.setattr(
            sys,
            "pycache_prefix",
            wrong if mismatched_layer == "runtime" else str(prefix),
        )
        monkeypatch.setattr(
            sys,
            "_xoptions",
            {
                "pycache_prefix": (
                    wrong if mismatched_layer == "xoption" else str(prefix)
                )
            },
        )
        monkeypatch.setenv(
            "PYTHONPYCACHEPREFIX",
            wrong if mismatched_layer == "environment" else str(prefix),
        )
        assert (
            capsule_module._pycache_prefix_contract(root)[
                "pycache_prefix_exact"
            ]
            is False
        )
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    monkeypatch.setattr(sys, "_xoptions", {"pycache_prefix": str(prefix)})
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(prefix))
    prefix.chmod(0o700)
    prefix.rmdir()

    target = root / ".venv" / "linked-cache"
    target.mkdir(mode=0o500)
    prefix.symlink_to(target, target_is_directory=True)
    linked = capsule_module._pycache_prefix_contract(root)
    assert linked["pycache_prefix_not_symlink"] is False
    prefix.unlink()

    external = tmp_path / "external-cache"
    external.mkdir(mode=0o500)
    monkeypatch.setattr(sys, "pycache_prefix", str(external))
    monkeypatch.setattr(sys, "_xoptions", {"pycache_prefix": str(external)})
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(external))
    escaped = capsule_module._pycache_prefix_contract(root)
    assert escaped["pycache_prefix_exact"] is False
    assert escaped["pycache_prefix_inside_capsule"] is False
    target.chmod(0o700)
    external.chmod(0o700)


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


def test_tree_manifest_ignores_concurrently_mutating_derived_bytecode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    source = root / "package" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    expected = _tree_manifest_hash(root)

    cache = source.parent / "__pycache__"
    cache.mkdir()
    cached = cache / "module.cpython-312.pyc"
    cached_optimized = cache / "module.cpython-312.opt-1.pyc"
    legacy_cached = cache / "module.pyo"
    started = threading.Event()
    stop = threading.Event()

    def mutate_bytecode() -> None:
        generation = 0
        started.set()
        while not stop.is_set():
            payload = f"derived-{generation}".encode()
            cached.write_bytes(payload)
            cached_optimized.write_bytes(payload)
            legacy_cached.write_bytes(payload)
            cached.unlink(missing_ok=True)
            cached_optimized.unlink(missing_ok=True)
            legacy_cached.unlink(missing_ok=True)
            generation += 1

    worker = threading.Thread(target=mutate_bytecode)
    worker.start()
    assert started.wait(timeout=1)
    try:
        observed = {_tree_manifest_hash(root) for _ in range(20)}
    finally:
        stop.set()
        worker.join(timeout=1)

    assert not worker.is_alive()
    assert observed == {expected}


@pytest.mark.parametrize("suffix", [".pyc", ".pyo"])
def test_tree_manifest_tracks_sourceless_bytecode_outside_cache(
    tmp_path: Path,
    suffix: str,
) -> None:
    root = tmp_path / "runtime"
    bytecode = root / f"plugin{suffix}"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"executable-bytecode-v1")
    before = _tree_manifest_hash(root)

    bytecode.write_bytes(b"executable-bytecode-v2")

    assert _tree_manifest_hash(root) != before


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


def test_provision_installs_dependencies_without_the_project_and_writes_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    model_config = repo / "config" / "llm.yaml"
    model_config.parent.mkdir(parents=True)
    model_config.write_text("summary: {}\n", encoding="utf-8")
    uv = tmp_path / "trusted" / "uv"
    uv.parent.mkdir()
    uv.write_text("trusted uv\n", encoding="utf-8")
    expected_commit = "a" * 40
    temporary_root = tmp_path / "private"
    commands: list[list[str]] = []
    checkout_checks: list[str] = []
    prefix_before_freeze: dict[str, object] = {}

    def materialize(root: Path, _commit: str) -> None:
        runner = (
            root
            / "applications"
            / "memory_feature_validation"
            / "scripts"
            / "run_memory_review_campaign.py"
        )
        runner.parent.mkdir(parents=True)
        runner.write_text("pass\n", encoding="utf-8")
        package = root / "src"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(
            "import sys\n"
            "from pathlib import Path\n\n"
            "def main():\n"
            "    root = Path(__file__).resolve().parents[1]\n"
            "    prefix = root / '.venv' / '.agentloom-pycache'\n"
            "    if sys.pycache_prefix != str(prefix):\n"
            "        return 91\n"
            "    if sys.dont_write_bytecode is not True:\n"
            "        return 92\n"
            "    if any(prefix.iterdir()):\n"
            "        return 93\n"
            "    print('NESTED_LAUNCH_OK')\n"
            "    return 0\n",
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text(
            '[project.scripts]\nloom = "src.__main__:main"\n',
            encoding="utf-8",
        )
        (root / "nested" / "cwd").mkdir(parents=True)

    def run_checked(command, *, cwd, **_kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        if len(command) > 1 and command[1] == "sync":
            if "--check" in command:
                assert (cwd / ".venv" / "bin" / "loom").is_file()
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            bin_dir = cwd / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").symlink_to(Path(sys.executable).resolve())
            if "--no-install-project" not in command:
                (cwd / "AgentLoom.egg-info").mkdir()
        stdout = expected_commit + "\n" if "rev-parse" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def cleanup(_repo_root: Path, private: Path, _capsule: Path) -> None:
        capsule_module._thaw_tree(private)
        capsule_module.shutil.rmtree(private)

    def make_private_root(**_kwargs) -> str:
        temporary_root.mkdir()
        return str(temporary_root)

    def checkout_state(root: Path, commit: str) -> tuple[bool, str]:
        assert (root / ".venv" / "bin" / "loom").is_file()
        assert not list(root.glob("*.egg-info"))
        checkout_checks.append(commit)
        return True, "b" * 64

    real_freeze_capsule = capsule_module._freeze_capsule

    def freeze_capsule(root: Path) -> None:
        prefix = root / ".venv" / ".agentloom-pycache"
        metadata = prefix.lstat()
        prefix_before_freeze.update(
            mode=stat.S_IMODE(metadata.st_mode),
            is_symlink=prefix.is_symlink(),
            empty=not any(prefix.iterdir()),
        )
        real_freeze_capsule(root)

    monkeypatch.setattr(capsule_module, "_require_isolated_git_metadata", lambda _: [])
    monkeypatch.setattr(capsule_module, "_trusted_uv", lambda: uv)
    monkeypatch.setattr(capsule_module, "_unsafe_git_customization_issues", lambda _: [])
    monkeypatch.setattr(capsule_module.tempfile, "mkdtemp", make_private_root)
    monkeypatch.setattr(capsule_module, "_materialize_commit_tree", materialize)
    monkeypatch.setattr(capsule_module, "_run_checked", run_checked)
    monkeypatch.setattr(capsule_module, "_checkout_state", checkout_state)
    monkeypatch.setattr(capsule_module, "_verify_write_guard", lambda _: None)
    monkeypatch.setattr(capsule_module, "_freeze_capsule", freeze_capsule)
    monkeypatch.setattr(capsule_module, "_secure_cleanup", cleanup)

    with capsule_module.provision_capsule(
        repo,
        expected_commit=expected_commit,
    ) as capsule:
        sync_commands = [command for command in commands if "sync" in command]
        assert sync_commands == [
            [
                str(uv),
                "sync",
                "--locked",
                "--all-groups",
                "--quiet",
                "--no-install-project",
                "--python",
                str(Path(sys.executable).resolve()),
            ],
            [
                str(uv),
                "sync",
                "--locked",
                "--all-groups",
                "--no-install-project",
                "--check",
            ],
        ]
        assert not list(capsule.root.glob("*.egg-info"))
        loom = capsule.root / ".venv" / "bin" / "loom"
        pycache_prefix = capsule.root / ".venv" / ".agentloom-pycache"
        assert prefix_before_freeze == {
            "mode": 0o700,
            "is_symlink": False,
            "empty": True,
        }
        assert capsule.env["PYTHONPYCACHEPREFIX"] == str(pycache_prefix)
        assert pycache_prefix.is_dir() and not pycache_prefix.is_symlink()
        assert pycache_prefix.stat().st_mode & 0o222 == 0
        assert not any(pycache_prefix.iterdir())
        assert loom.is_file() and not loom.is_symlink()
        assert loom.read_text(encoding="utf-8") == (
            f"#!{capsule.python}\n"
            "import sys\n\n"
            f"_EXPECTED_PYCACHE_PREFIX = {str(pycache_prefix)!r}\n"
            "if (\n"
            "    sys.pycache_prefix != _EXPECTED_PYCACHE_PREFIX\n"
            "    or sys.dont_write_bytecode is not True\n"
            "):\n"
            "    raise RuntimeError(\"capsule bytecode isolation was invalid\")\n"
            "from importlib.util import find_spec\n"
            "from pathlib import Path\n"
            "\n"
            "_CAPSULE_ROOT = Path(__file__).resolve().parents[2]\n"
            "sys.path.append(str(_CAPSULE_ROOT))\n"
            "_SRC_SPEC = find_spec(\"src\")\n"
            "if (\n"
            "    _SRC_SPEC is None\n"
            "    or _SRC_SPEC.origin is None\n"
            "    or Path(_SRC_SPEC.origin).resolve()\n"
            "    != (_CAPSULE_ROOT / \"src\" / \"__init__.py\").resolve()\n"
            "):\n"
            "    raise RuntimeError(\"capsule src import origin was invalid\")\n"
            "from src.__main__ import main\n\n"
            "if __name__ == \"__main__\":\n"
            "    sys.exit(main())\n"
        )
        mode = loom.stat().st_mode
        assert mode & 0o111 == 0o111
        assert mode & 0o222 == 0
        assert loom.stat().st_nlink == 1
        completed = subprocess.run(
            [str(loom)],
            cwd=capsule.root / "nested" / "cwd",
            env=capsule.env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "NESTED_LAUNCH_OK"
        assert not any(pycache_prefix.iterdir())
        assert checkout_checks == [expected_commit]


def test_trusted_launcher_creation_is_exclusive(tmp_path: Path) -> None:
    root = tmp_path / "capsule"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python\n", encoding="utf-8")
    loom = python.parent / "loom"
    loom.write_text("untrusted launcher\n", encoding="utf-8")
    original_inode = loom.stat().st_ino

    with pytest.raises(RuntimeError, match="already existed"):
        capsule_module._write_trusted_loom_launcher(root, python)

    assert loom.read_text(encoding="utf-8") == "untrusted launcher\n"
    assert loom.stat().st_ino == original_inode


def test_trusted_launcher_checks_exact_pycache_prefix_before_other_imports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capsule"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    (root / "pyproject.toml").write_text(
        '[project.scripts]\nloom = "src.__main__:main"\n',
        encoding="utf-8",
    )
    imported = root / "src-imported"
    package = root / "src"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(imported)!r}).touch()\n",
        encoding="utf-8",
    )
    (package / "__main__.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    launcher = capsule_module._write_trusted_loom_launcher(root, python)
    payload = launcher.read_text(encoding="utf-8")

    assert payload.splitlines()[1] == "import sys"
    prefix_check = payload.index("sys.pycache_prefix")
    assert prefix_check < payload.index("from importlib.util import find_spec")
    assert prefix_check < payload.index("from pathlib import Path")

    env = os.environ.copy()
    env.pop("PYTHONPYCACHEPREFIX", None)
    rejected = subprocess.run(
        [str(launcher)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rejected.returncode != 0
    assert not imported.exists()


def test_trusted_launcher_rejects_drifted_project_entrypoint(tmp_path: Path) -> None:
    root = tmp_path / "capsule"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project.scripts]\nloom = "unexpected.module:main"\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="entrypoint contract"):
        capsule_module._write_trusted_loom_launcher(root, python)

    assert not (python.parent / "loom").exists()


def test_descriptor_checks_the_locked_dependency_only_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    uv = tmp_path / "uv"
    uv.write_text("trusted uv\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run_checked(command, **_kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        stdout = "uv 0.test\n" if command[-1] == "--version" else "ok\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setenv(capsule_module.CAPSULE_UV_BINARY_ENV, str(uv))
    monkeypatch.setattr(capsule_module, "_run_checked", run_checked)
    monkeypatch.setattr(capsule_module, "_tree_manifest_hash", lambda *_args, **_kwargs: "c" * 64)
    monkeypatch.setattr(capsule_module, "_checkout_state", lambda *_args: (True, "d" * 64))
    monkeypatch.setattr(capsule_module, "_tree_is_read_only", lambda _: True)
    monkeypatch.setattr(capsule_module, "_regular_files_unshared", lambda _: True)
    monkeypatch.setattr(capsule_module, "_git_metadata_paths", lambda _: [tmp_path])
    monkeypatch.setattr(capsule_module, "_git_metadata_is_isolated", lambda _: True)
    monkeypatch.setattr(capsule_module, "active_capsule_bootstrap_issues", lambda _: [])
    monkeypatch.setenv(
        "PYTHONPYCACHEPREFIX",
        "/private/ephemeral-capsule-a/.venv/.agentloom-pycache",
    )

    descriptor = capsule_module.build_capsule_descriptor(
        repo_root=REPO_ROOT,
        runner_file=REPO_ROOT
        / "applications"
        / "memory_feature_validation"
        / "scripts"
        / "run_memory_review_campaign.py",
        source={"commit": "a" * 40, "files": []},
        dataset={"files": []},
        model_contract={"configured": True},
        model_config_memory_only=True,
    )
    commands.clear()
    monkeypatch.setenv(
        "PYTHONPYCACHEPREFIX",
        "/private/ephemeral-capsule-b/.venv/.agentloom-pycache",
    )
    relocated_descriptor = capsule_module.build_capsule_descriptor(
        repo_root=REPO_ROOT,
        runner_file=REPO_ROOT
        / "applications"
        / "memory_feature_validation"
        / "scripts"
        / "run_memory_review_campaign.py",
        source={"commit": "a" * 40, "files": []},
        dataset={"files": []},
        model_contract={"configured": True},
        model_config_memory_only=True,
    )

    assert descriptor["lock_sync_ok"] is True
    assert descriptor["checkout_exact"] is True
    assert descriptor["loom_is_capsule"] is True
    assert descriptor["loom_shebang_is_capsule"] is True
    assert descriptor["loom_shebang_matches_python"] is True
    assert descriptor["src_origin_is_capsule"] is True
    assert (
        descriptor["runtime_env_contract_hash"]
        == relocated_descriptor["runtime_env_contract_hash"]
    )
    assert [command for command in commands if "sync" in command] == [
        [
            str(uv),
            "sync",
            "--locked",
            "--all-groups",
            "--no-install-project",
            "--check",
        ]
    ]


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
    assert env["PYTHONPYCACHEPREFIX"] == str(
        tmp_path / "capsule" / ".venv" / ".agentloom-pycache"
    )


def test_private_prefix_prevents_poisoned_in_tree_bytecode_execution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "poisoned_module.py"
    source.write_text("print('EVIL_BYTECODE')\n", encoding="utf-8")
    cached = Path(py_compile.compile(
        str(source),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ))
    assert "__pycache__" in cached.parts
    source.write_text("print('SAFE_SOURCE')\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPYCACHEPREFIX", None)

    poisoned = subprocess.run(
        [sys.executable, "-B", "-c", "import poisoned_module"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    private_prefix = tmp_path / "private-pycache"
    private_prefix.mkdir()
    isolated = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            f"pycache_prefix={private_prefix}",
            "-c",
            "import poisoned_module",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert poisoned.returncode == 0, poisoned.stderr
    assert poisoned.stdout.strip() == "EVIL_BYTECODE"
    assert isolated.returncode == 0, isolated.stderr
    assert isolated.stdout.strip() == "SAFE_SOURCE"
    assert not any(private_prefix.iterdir())


def test_isolated_python_ignores_env_pycache_prefix_without_xoption(
    tmp_path: Path,
) -> None:
    env_prefix = tmp_path / "ignored-env-prefix"
    exact_prefix = tmp_path / "explicit-xoption-prefix"
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(env_prefix)}
    probe = "import sys; print(repr(sys.pycache_prefix))"

    ignored = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    explicit = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            f"pycache_prefix={exact_prefix}",
            "-c",
            probe,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert ignored.returncode == 0, ignored.stderr
    assert ignored.stdout.strip() == "None"
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() == repr(str(exact_prefix))


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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS release guard")
def test_runtime_guard_allows_system_dns_socket(tmp_path: Path) -> None:
    protected = tmp_path / "capsule"
    protected.mkdir()
    mdns_socket = Path("/private/var/run/mDNSResponder")
    assert stat.S_ISSOCK(mdns_socket.stat().st_mode)

    completed = subprocess.run(
        guarded_runtime_command(
            [
                sys.executable,
                "-c",
                (
                    "import socket,sys;"
                    "s=socket.socket(socket.AF_UNIX);"
                    "s.connect(sys.argv[1]);s.close()"
                ),
                str(mdns_socket),
            ],
            repo_root=protected,
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


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
