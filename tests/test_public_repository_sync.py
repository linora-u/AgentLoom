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


def test_only_the_explicit_application_is_private():
    tree = {
        "applications/news_agent/run.py": ("100644", "private"),
        "applications/news_agent/data/source.parquet": ("100644", "private-data"),
        "docs/research/news_notes.md": ("100644", "public-doc"),
        "patches/news_agent.patch": ("100644", "public-patch"),
        "applications/news_agent_demo/run.py": ("100644", "public-demo"),
        "src/framework.py": ("100644", "framework"),
    }
    exported = sync.public_tree(tree)
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
        sync.validate_public_path(path, mode)


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
    desired = sync.public_tree(sync.local_tree("HEAD"))
    sync.materialize_public_tree("HEAD", desired, public)
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


def test_already_published_tree_resumes_without_a_duplicate_public_pr(monkeypatch):
    coordinator, pr = coordinator_fixture(monkeypatch)
    monkeypatch.setattr(sync, "local_tree", lambda ref: {"src/public.py": ("100644", ref)})
    coordinator.remote_tree = Mock(return_value={"src/public.py": ("100644", "head")})
    coordinator.ensure_public_pr = Mock(side_effect=AssertionError("duplicate publication"))
    coordinator.process(pr)
    coordinator.merge_private.assert_called_once_with(1, "head")
