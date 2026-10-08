"""Verify export isolation and that publication failures cannot merge private PRs."""

import importlib.util
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).parents[1] / ".github/scripts/public_sync.py"
spec = importlib.util.spec_from_file_location("public_sync", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
PRIVATE_PATHS = ("applications/news_agent/",)


def test_only_the_explicit_application_is_private():
    tree = {
        "applications/news_agent/run.py": ("100644", "private"),
        "applications/news_agent/data/source.parquet": ("100644", "private-data"),
        "docs/research/news_notes.md": ("100644", "public-doc"),
        "patches/news_agent.patch": ("100644", "public-patch"),
        "applications/news_agent_demo/run.py": ("100644", "public-demo"),
        "src/framework.py": ("100644", "framework"),
    }
    exported = sync.public_tree(tree, PRIVATE_PATHS)
    assert set(tree) - set(exported) == {
        "applications/news_agent/run.py", "applications/news_agent/data/source.parquet"
    }


@pytest.mark.parametrize("path,mode", [
    ("applications/news_agent/run.py", "100644"),
    ("../outside", "100644"), ("/outside", "100644"),
    (".git/config", "100644"), ("src/symlink", "120000"),
])
def test_export_rejects_private_or_unsafe_entries(path, mode):
    with pytest.raises(RuntimeError):
        sync.validate_public_path(path, mode, PRIVATE_PATHS)


def test_public_commit_keeps_public_ancestry_and_exact_file_modes(tmp_path, monkeypatch):
    source, public = tmp_path / "source", tmp_path / "public"
    for repo in [source, public]:
        repo.mkdir()
        sync.command("git", "init", "-b", "main", cwd=repo)
        sync.command("git", "config", "user.name", "Fixture", cwd=repo)
        sync.command("git", "config", "user.email", "fixture@example.test", cwd=repo)
    (source / "applications/news_agent").mkdir(parents=True)
    (source / "applications/news_agent/private.py").write_text("PRIVATE_ONLY = True\n")
    (source / "public.sh").write_text("#!/bin/sh\necho public\n")
    (source / "public.sh").chmod(0o755)
    sync.command("git", "add", ".", cwd=source)
    sync.command("git", "commit", "-m", "Private source message", cwd=source)
    (public / "obsolete.txt").write_text("removed\n")
    sync.command("git", "add", ".", cwd=public)
    sync.command("git", "commit", "-m", "Public parent", cwd=public)
    parent = sync.command("git", "rev-parse", "HEAD", cwd=public).strip()
    monkeypatch.chdir(source)
    desired = sync.public_tree(sync.local_tree("HEAD"), PRIVATE_PATHS)
    sync.materialize_public_tree("HEAD", desired, public, PRIVATE_PATHS)
    sync.command("git", "commit", "-m", "Sync public files", cwd=public)
    assert sync.local_tree("HEAD", public) == desired
    assert sync.command("git", "rev-parse", "HEAD^", cwd=public).strip() == parent
    assert not (public / "applications/news_agent").exists()
    assert not (public / "obsolete.txt").exists()


def coordinator_fixture(monkeypatch):
    config = {"private_repository": "owner/private", "public_repository": "owner/public",
              "required_public_checks": ["Python tests"]}
    coordinator = sync.Coordinator(config, wait_seconds=0)
    coordinator.status = Mock()
    coordinator.verify_private_head = Mock()
    coordinator.merge_private = Mock()
    monkeypatch.setattr(sync, "command", Mock())
    monkeypatch.setattr(sync, "prefixes_at_ref", lambda ref: PRIVATE_PATHS)
    monkeypatch.setattr(subprocess, "run", Mock(return_value=Mock(returncode=0)))
    return coordinator, {"number": 1, "head": {"sha": "head"}, "base": {"sha": "base"}}


def test_private_only_change_needs_no_public_pr_or_tests(monkeypatch):
    coordinator, pr = coordinator_fixture(monkeypatch)
    monkeypatch.setattr(sync, "local_tree", lambda ref: {
        "src/public.py": ("100644", "same"),
        "applications/news_agent/run.py": ("100644", ref),
    })
    coordinator.remote_tree = Mock(side_effect=AssertionError("public API must not be needed"))
    coordinator.process(pr)
    coordinator.merge_private.assert_called_once_with(1, "head")
    assert coordinator.status.call_args.args[1] == "success"


def test_public_failure_never_merges_private_pr(monkeypatch):
    coordinator, pr = coordinator_fixture(monkeypatch)
    monkeypatch.setattr(sync, "local_tree", lambda ref: {"src/public.py": ("100644", ref)})
    coordinator.remote_tree = Mock(return_value={"src/public.py": ("100644", "base")})
    coordinator.ensure_public_pr = Mock(return_value={"html_url": "https://example.test/pr/1"})
    coordinator.wait_public_checks = Mock(side_effect=RuntimeError("Public CI failed"))
    with pytest.raises(RuntimeError, match="Public CI failed"):
        coordinator.process(pr)
    coordinator.merge_private.assert_not_called()


def test_independent_public_changes_are_not_overwritten(monkeypatch):
    coordinator, pr = coordinator_fixture(monkeypatch)
    monkeypatch.setattr(sync, "local_tree", lambda ref: {"src/public.py": ("100644", ref)})
    coordinator.remote_tree = Mock(return_value={"external.py": ("100644", "contribution")})
    coordinator.ensure_public_pr = Mock()
    with pytest.raises(RuntimeError, match="diverged"):
        coordinator.process(pr)
    coordinator.ensure_public_pr.assert_not_called()
    coordinator.merge_private.assert_not_called()


def test_updated_private_head_invalidates_public_merge(monkeypatch):
    coordinator = sync.Coordinator({"private_repository": "owner/private", "public_repository": "owner/public",
                                    "required_public_checks": []})
    coordinator.private_pr = Mock(return_value={"state": "open", "draft": False,
                                               "head": {"sha": "new-head"}, "base": {"sha": "base"}})
    with pytest.raises(RuntimeError, match="changed"):
        coordinator.verify_private_head(1, "old-head", "base")


def test_public_guard_rejects_private_history_after_files_are_deleted(tmp_path):
    sync.command("git", "init", "-b", "main", cwd=tmp_path)
    sync.command("git", "config", "user.name", "Fixture", cwd=tmp_path)
    sync.command("git", "config", "user.email", "fixture@example.test", cwd=tmp_path)
    private = tmp_path / "applications/news_agent/private.py"
    private.parent.mkdir(parents=True)
    private.write_text("PRIVATE_ONLY = True\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/public-sync.json").write_text('{"private_prefixes": ["applications/news_agent/"]}')
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Private ancestor", cwd=tmp_path)
    sync.command("git", "rm", "-r", "applications", cwd=tmp_path)
    (tmp_path / "public.py").write_text("PUBLIC_ONLY = True\n")
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Clean current tree", cwd=tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT.with_name("check_public_tree.py"))],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "history contains private" in result.stderr


def test_configuration_excludes_added_directories_and_exact_files():
    prefixes = sync.configured_prefixes({"private_prefixes": ["applications/news_agent/", "internal/", "config/secret.yaml"]})
    tree = {path: ("100644", path) for path in [
        "applications/news_agent/run.py", "internal/strategy.py", "config/secret.yaml",
        "internal_demo/public.py", "config/secret.yaml.example", "src/public.py",
    ]}
    assert set(sync.public_tree(tree, prefixes)) == {
        "internal_demo/public.py", "config/secret.yaml.example", "src/public.py"
    }
    with pytest.raises(RuntimeError, match="Configured private"):
        sync.validate_public_path("internal/strategy.py", "100644", prefixes)


@pytest.mark.parametrize("prefix", ["", "/outside", "../outside", ".git/config", "internal/*", ".", "a\nb"])
def test_invalid_private_configuration_is_rejected(prefix):
    with pytest.raises(RuntimeError):
        sync.configured_prefixes({"private_prefixes": [prefix]})


def test_new_private_path_takes_effect_in_the_same_pr(tmp_path, monkeypatch):
    sync.command("git", "init", "-b", "main", cwd=tmp_path)
    sync.command("git", "config", "user.name", "Fixture", cwd=tmp_path)
    sync.command("git", "config", "user.email", "fixture@example.test", cwd=tmp_path)
    config = tmp_path / ".github/public-sync.json"
    config.parent.mkdir()
    config.write_text('{"private_prefixes": ["applications/news_agent/"]}')
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Base configuration", cwd=tmp_path)
    base = sync.command("git", "rev-parse", "HEAD", cwd=tmp_path).decode().strip()
    config.write_text('{"private_prefixes": ["applications/news_agent/", "internal/"]}')
    secret = tmp_path / "internal/strategy.py"
    secret.parent.mkdir()
    secret.write_text("PRIVATE_ONLY = True\n")
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Add private directory and rule together", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    assert sync.prefixes_at_ref(base) == ("applications/news_agent",)
    head_prefixes = sync.prefixes_at_ref("HEAD")
    assert head_prefixes == ("applications/news_agent", "internal")
    assert "internal/strategy.py" not in sync.public_tree(sync.local_tree("HEAD"), head_prefixes)


def test_guard_uses_custom_configuration_and_requested_ref(tmp_path):
    sync.command("git", "init", "-b", "main", cwd=tmp_path)
    sync.command("git", "config", "user.name", "Fixture", cwd=tmp_path)
    sync.command("git", "config", "user.email", "fixture@example.test", cwd=tmp_path)
    (tmp_path / "public.py").write_text("PUBLIC_ONLY = True\n")
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Clean public ancestor", cwd=tmp_path)
    clean = sync.command("git", "rev-parse", "HEAD", cwd=tmp_path).decode().strip()
    private = tmp_path / "internal/strategy.py"
    private.parent.mkdir()
    private.write_text("PRIVATE_ONLY = True\n")
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Private custom directory", cwd=tmp_path)
    config = tmp_path / "rules.json"
    config.write_text('{"private_prefixes": ["internal/"]}')
    args = [sys.executable, str(SCRIPT.with_name("check_public_tree.py")), "--config", str(config)]
    rejected = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True)
    allowed = subprocess.run([*args, "--ref", clean], cwd=tmp_path, capture_output=True, text=True)
    assert rejected.returncode != 0 and "history contains private" in rejected.stderr
    assert allowed.returncode == 0


def test_already_published_tree_resumes_without_a_duplicate_public_pr(monkeypatch):
    coordinator, pr = coordinator_fixture(monkeypatch)
    monkeypatch.setattr(sync, "local_tree", lambda ref: {"src/public.py": ("100644", ref)})
    coordinator.remote_tree = Mock(return_value={"src/public.py": ("100644", "head")})
    coordinator.ensure_public_pr = Mock(side_effect=AssertionError("duplicate publication"))
    coordinator.process(pr)
    coordinator.merge_private.assert_called_once_with(1, "head")


def recovery_fixture(monkeypatch):
    coordinator, _ = coordinator_fixture(monkeypatch)
    desired = {"src/public.py": ("100644", "new-public")}
    monkeypatch.setattr(sync, "local_tree", lambda ref: {
        **desired, "applications/news_agent/private.py": ("100644", "private")
    })
    coordinator.private_main = Mock(return_value="main-head")
    coordinator.remote_tree = Mock(side_effect=[
        {"src/public.py": ("100644", "old-public")}, desired, desired, desired
    ])
    coordinator.find_public_base = Mock(return_value="published-base")
    public_pr = {"number": 7, "head": {"sha": "tested-public"}, "html_url": "https://example.test/pr/7"}
    coordinator.ensure_public_pr = Mock(return_value=public_pr)
    coordinator.wait_public_checks = Mock(return_value=public_pr)
    api = Mock(return_value={"merged": True, "sha": "public-merge"})
    monkeypatch.setattr(sync, "api", api)
    return coordinator, desired, api


def test_main_recovery_does_nothing_for_already_synced_or_private_only_updates(monkeypatch):
    coordinator, desired, api = recovery_fixture(monkeypatch)
    coordinator.remote_tree = Mock(return_value=desired)
    coordinator.reconcile_main()
    coordinator.ensure_public_pr.assert_not_called()
    coordinator.wait_public_checks.assert_not_called()
    coordinator.merge_private.assert_not_called()
    api.assert_not_called()


def test_manual_private_merge_recovers_only_public_files_after_exact_head_checks(monkeypatch):
    coordinator, desired, api = recovery_fixture(monkeypatch)
    coordinator.reconcile_main()
    coordinator.ensure_public_pr.assert_called_once_with(
        None, "main-head", "published-base", desired,
        {"src/public.py": ("100644", "old-public")}, PRIVATE_PATHS,
    )
    api.assert_called_once_with("repos/owner/public/pulls/7/merge", "PUT",
                               {"sha": "tested-public", "merge_method": "squash"})
    coordinator.merge_private.assert_not_called()
    assert coordinator.status.call_args.args[1] == "success"


def test_recovery_public_ci_failure_cannot_merge_public(monkeypatch):
    coordinator, _, api = recovery_fixture(monkeypatch)
    coordinator.wait_public_checks.side_effect = RuntimeError("Public CI failed")
    with pytest.raises(RuntimeError, match="Public CI failed"):
        coordinator.reconcile_main()
    api.assert_not_called()
    assert coordinator.status.call_args.args[1] == "failure"


def test_private_main_update_during_recovery_invalidates_public_merge(monkeypatch):
    coordinator, _, api = recovery_fixture(monkeypatch)
    coordinator.private_main.side_effect = ["main-head", "new-main-head"]
    with pytest.raises(RuntimeError, match="Private main moved"):
        coordinator.reconcile_main()
    api.assert_not_called()
    coordinator.merge_private.assert_not_called()


def test_recovery_rejects_independent_public_changes_before_publishing(monkeypatch):
    coordinator, _, api = recovery_fixture(monkeypatch)
    coordinator.find_public_base.side_effect = sync.PublicDivergence("Public main has independent changes")
    with pytest.raises(sync.PublicDivergence, match="independent changes"):
        coordinator.reconcile_main()
    coordinator.ensure_public_pr.assert_not_called()
    api.assert_not_called()


def test_recovery_finds_published_main_before_multiple_manual_updates(tmp_path, monkeypatch):
    sync.command("git", "init", "-b", "main", cwd=tmp_path)
    sync.command("git", "config", "user.name", "Fixture", cwd=tmp_path)
    sync.command("git", "config", "user.email", "fixture@example.test", cwd=tmp_path)
    config = tmp_path / ".github/public-sync.json"
    config.parent.mkdir()
    config.write_text('{"private_prefixes": ["applications/news_agent/"]}')
    private = tmp_path / "applications/news_agent/private.py"
    private.parent.mkdir(parents=True)
    private.write_text("private\n")
    public = tmp_path / "public.py"
    public.write_text("published\n")
    sync.command("git", "add", ".", cwd=tmp_path)
    sync.command("git", "commit", "-m", "Published baseline", cwd=tmp_path)
    base = sync.command("git", "rev-parse", "HEAD", cwd=tmp_path).decode().strip()
    actual = sync.public_tree(sync.local_tree("HEAD", tmp_path), PRIVATE_PATHS)
    for contents in ["manual update one\n", "manual update two\n"]:
        public.write_text(contents)
        sync.command("git", "add", ".", cwd=tmp_path)
        sync.command("git", "commit", "-m", "Manual private main update", cwd=tmp_path)
    coordinator = sync.Coordinator({"private_repository": "owner/private", "public_repository": "owner/public",
                                    "required_public_checks": []})
    monkeypatch.chdir(tmp_path)
    assert coordinator.find_public_base("HEAD", actual) == base
    with pytest.raises(sync.PublicDivergence, match="independent changes"):
        coordinator.find_public_base("HEAD", {"public.py": ("100644", "independent-edit")})
