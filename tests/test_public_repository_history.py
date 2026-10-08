"""Exercise filtered history with real Git objects and publication retry boundaries."""

import base64
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).parents[1] / ".github/scripts/public_sync.py"
spec = importlib.util.spec_from_file_location("public_sync_history", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
PRIVATE = ("applications/news_agent",)
CONFIG = {"private_repository": "owner/private", "public_repository": "owner/public",
          "required_public_checks": []}


def commit(repo, message, *, author="Fixture", email="fixture@example.test", date="1700000000 +0800"):
    sync.command("git", "add", "-A", cwd=repo)
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date})
    sync.command("git", "commit", "--cleanup=verbatim", "-F", "-", cwd=repo,
                 data=message.encode(), env=env)
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
        (repo / "public.txt").write_text("baseline\n")
    private = source / "applications/news_agent/private.txt"
    private.parent.mkdir(parents=True)
    private.write_text("PRIVATE_INITIAL\n")
    base = commit(source, "Private baseline\n")
    commit(public, "Public baseline\n")
    monkeypatch.chdir(source)
    return source, public, base


def export(repositories, tmp_path, *, base=None, head="HEAD", prefixes=PRIVATE):
    source, public, initial = repositories
    target = tmp_path / "export"
    sync.command("git", "clone", "--no-checkout", str(public), str(target))
    history = sync.public_history(base or initial, head, prefixes)
    mapping, title = sync.export_public_history(history, target, prefixes)
    assert sync.local_tree("HEAD", target) == sync.public_tree(sync.local_tree(head, source), prefixes)
    return mapping, title, target


def raw_message(repo, sha):
    return sync.command("git", "cat-file", "commit", sha, cwd=repo).split(b"\n\n", 1)[1]


def body(base, head, commits=None):
    return sync.HISTORY_MARKER + json.dumps({"version": 1, "source_base": base,
                                             "source_head": head, "commits": commits or []}) + "\n-->\n"


def test_mixed_commits_preserve_messages_authors_dates_modes_and_exclude_private_objects(repositories, tmp_path):
    source, public, base = repositories
    (source / "public.txt").write_text("first public change\n")
    (source / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o755)
    secret = source / "applications/news_agent/private.txt"
    secret.write_text("PRIVATE_MIXED\n")
    first = commit(source, "修复运行时\n\nFull body with trailing spaces.  \n\nCo-authored-by: Peer <peer@example.test>\n",
                   author="原作者", email="author@example.test")
    secret.write_text("PRIVATE_ONLY\n")
    skipped = commit(source, "Private-only strategy\n")
    # Exercise file/directory replacement and deletion during consecutive snapshots.
    (source / "public.txt").unlink()
    (source / "public.txt").mkdir()
    (source / "public.txt/renamed.txt").write_text("second public change\n")
    (source / "run.sh").unlink()
    last = commit(source, "Move public file\n\nRetain the complete second body.\n",
                  author="Contributor", email="contributor@example.test", date="1700000123 -0500")
    mapping, title, target = export(repositories, tmp_path)
    assert [item["source"] for item in mapping if item["exported"]] == [first, last]
    assert mapping[1] == {"source": skipped, "public": mapping[0]["public"], "exported": False}
    assert title == "Move public file"
    for item in (mapping[0], mapping[2]):
        assert raw_message(target, item["public"]) == raw_message(source, item["source"])
        assert sync.command("git", "show", "-s", "--format=%an%n%ae%n%aI", item["public"], cwd=target) == \
            sync.command("git", "show", "-s", "--format=%an%n%ae%n%aI", item["source"], cwd=source)
        assert sync.command("git", "show", "-s", "--format=%cn%n%ce", item["public"], cwd=target).decode() == \
            "linora-u\n260928258+linora-u@users.noreply.github.com\n"
        assert sync.local_tree(item["public"], target) == sync.public_tree(sync.local_tree(item["source"]), PRIVATE)
    assert sync.local_tree(mapping[0]["public"], target)["run.sh"][0] == "100755"
    assert sync.is_ancestor(sync.command("git", "rev-parse", "HEAD", cwd=public).decode().strip(), "HEAD", target)
    for ref in (base, first, skipped, last, f"{last}:applications/news_agent/private.txt"):
        sha = sync.command("git", "rev-parse", ref, cwd=source).decode().strip()
        result = subprocess.run(["git", "cat-file", "-e", sha], cwd=target, capture_output=True)
        assert result.returncode != 0
    assert not sync.command("git", "rev-list", "HEAD", "--", *PRIVATE, cwd=target).strip()


def test_private_rule_added_later_hides_earlier_unpublished_content(repositories, tmp_path):
    source, _, _ = repositories
    secret = source / "internal/strategy.txt"
    secret.parent.mkdir()
    secret.write_text("UNPUBLISHED_SECRET\n")
    (source / "public.txt").write_text("public change\n")
    first = commit(source, "Add public feature and internal file\n")
    (source / sync.CONFIG_PATH).write_text(json.dumps({"private_prefixes": [*PRIVATE, "internal"]}))
    commit(source, "Mark internal files private\n")
    mapping, _, target = export(repositories, tmp_path, prefixes=(*PRIVATE, "internal"))
    assert mapping[0]["source"] == first
    assert not sync.command("git", "rev-list", "HEAD", "--", "internal", cwd=target).strip()
    for entry in mapping:
        assert "internal/strategy.txt" not in sync.local_tree(entry["public"], target)


def test_conflict_resolution_preserves_both_public_branches_and_merge_message(repositories, tmp_path):
    source, _, base = repositories
    sync.command("git", "checkout", "-b", "feature", cwd=source)
    (source / "public.txt").write_text("feature\n")
    feature = commit(source, "Feature branch change\n")
    sync.command("git", "checkout", "main", cwd=source)
    (source / "public.txt").write_text("main change\n")
    main = commit(source, "Main branch change\n")
    result = subprocess.run(["git", "merge", "--no-commit", "feature"], cwd=source, capture_output=True)
    assert result.returncode == 1
    (source / "public.txt").write_text("resolved combination\n")
    merge = commit(source, "Resolve public conflict\n\nKeep both branch changes.\n")
    mapping, _, target = export(repositories, tmp_path)
    by_source = {entry["source"]: entry["public"] for entry in mapping}
    parents = sync.command("git", "show", "-s", "--format=%P", by_source[merge], cwd=target).decode().split()
    assert parents == [by_source[main], by_source[feature]]
    assert raw_message(target, by_source[merge]) == raw_message(source, merge)
    assert len(mapping) == 3
    assert all(item["exported"] for item in mapping)
    assert subprocess.run(["git", "cat-file", "-e", base], cwd=target, capture_output=True).returncode != 0


def test_recovery_collapses_private_merge_wrapper_without_losing_feature_commits(repositories, tmp_path):
    source, _, _ = repositories
    sync.command("git", "checkout", "-b", "feature", cwd=source)
    (source / "public.txt").write_text("feature\n")
    feature = commit(source, "Actual feature change\n\nOriginal details.\n")
    sync.command("git", "checkout", "main", cwd=source)
    sync.command("git", "merge", "--no-ff", "feature", "-m", "Private PR merge wrapper", cwd=source)
    mapping, title, target = export(repositories, tmp_path)
    assert [item["source"] for item in mapping if item["exported"]] == [feature]
    assert mapping[-1]["public"] == mapping[0]["public"]
    assert title == "Actual feature change"
    assert raw_message(target, "HEAD") == raw_message(source, feature)


def test_branch_started_before_current_base_uses_older_public_parent(repositories, tmp_path):
    source, public, initial = repositories
    old_public = sync.command("git", "rev-parse", "HEAD", cwd=public).decode().strip()
    sync.command("git", "branch", "feature", initial, cwd=source)
    (source / "main-only.txt").write_text("already published\n")
    base = commit(source, "Earlier main change\n")
    (public / "main-only.txt").write_text("already published\n")
    commit(public, "Published earlier main change\n")
    sync.command("git", "checkout", "feature", cwd=source)
    (source / "public.txt").write_text("feature\n")
    feature = commit(source, "Feature edit\n")
    sync.command("git", "merge", "main", "-m", "Update feature with main", cwd=source)
    mapping, _, target = export(repositories, tmp_path, base=base)
    public_feature = next(item["public"] for item in mapping if item["source"] == feature)
    assert sync.command("git", "rev-parse", f"{public_feature}^", cwd=target).decode().strip() == old_public
    assert sync.command("git", "diff-tree", "--no-commit-id", "--name-only", "-r", public_feature,
                        cwd=target).decode().splitlines() == ["public.txt"]
    assert "main-only.txt" in sync.local_tree("HEAD", target)


def test_push_succeeds_then_pr_creation_failure_retries_identical_commits(repositories, monkeypatch):
    source, public, base = repositories
    (source / "public.txt").write_text("new public\n")
    head = commit(source, "Original feature title\n\nOriginal body.\n")
    desired = sync.public_tree(sync.local_tree(head), PRIVATE)
    actual = sync.local_tree("HEAD", public)
    real_command = sync.command
    pushed, created = [], []

    def offline_command(*args, cwd=None, data=None, env=None):
        if args[:2] == ("git", "clone"):
            return real_command("git", "clone", "--no-checkout", "--single-branch", "--branch", "main",
                                str(public), args[-1])
        if args[:2] == ("git", "push"):
            pushed.append(real_command("git", "rev-parse", "HEAD", cwd=cwd).decode().strip())
            return real_command("git", "push", str(public), args[-1], cwd=cwd)
        return real_command(*args, cwd=cwd, data=data, env=env)

    def fake_api(path, method="GET", payload=None):
        if method == "GET":
            return []
        created.append(payload)
        if len(created) == 1:
            raise RuntimeError("PR creation interrupted")
        return {"number": 3, **payload}

    monkeypatch.setattr(sync, "command", offline_command)
    monkeypatch.setattr(sync, "api", fake_api)
    coordinator = sync.Coordinator(CONFIG)
    coordinator.verify_private_head = Mock()
    coordinator._private_pr_title = "Meaningful private PR title"
    with pytest.raises(RuntimeError, match="creation interrupted"):
        coordinator.ensure_public_pr(1, head, base, desired, actual, PRIVATE)
    # Main may advance without changing files while the orphaned branch exists.
    sync.command("git", "commit", "--allow-empty", "-m", "Independent empty public commit", cwd=public)
    pr = coordinator.ensure_public_pr(1, head, base, desired, actual, PRIVATE)
    assert pushed[0] == pushed[1]
    assert pr["title"] == "Meaningful private PR title"
    assert sync.publication_metadata(pr["body"])["commits"] == [
        {"source": head, "public": pushed[0], "exported": True}
    ]
    assert coordinator.public_branch(1, head, base) != coordinator.public_branch(1, "f" * 40, base)


@pytest.mark.parametrize("merged", [False, True])
def test_existing_sync_pr_resumes_with_its_mapping_without_pushing(monkeypatch, merged):
    base, head = "a" * 40, "b" * 40
    pr = {"state": "closed" if merged else "open", "merged_at": "2026-10-08" if merged else None,
          "body": body(base, head), "head": {"sha": "c" * 40}}
    monkeypatch.setattr(sync, "api", Mock(return_value=[pr]))
    monkeypatch.setattr(sync, "command", Mock(side_effect=AssertionError("must not push")))
    coordinator = sync.Coordinator(CONFIG)
    coordinator.remote_tree = Mock(return_value={})
    assert coordinator.ensure_public_pr(1, head, base, {}, {}, PRIVATE) == pr


def test_private_cursor_tracks_reverted_public_changes_during_main_recovery(repositories, monkeypatch):
    source, public, base = repositories
    original = sync.local_tree("HEAD", public)
    (source / "public.txt").write_text("temporary change\n")
    commit(source, "Try public change\n")
    (source / "public.txt").write_text("baseline\n")
    head = commit(source, "Revert public change\n")
    coordinator = sync.Coordinator(CONFIG)
    state = {"source_head": base, "public_head": sync.command("git", "rev-parse", "HEAD", cwd=public).decode().strip()}
    coordinator.load_state = Mock(return_value=state)
    coordinator.public_main = Mock(return_value=state["public_head"])
    coordinator.remote_tree = Mock(return_value=original)
    coordinator.private_main = Mock(return_value=head)
    coordinator.status = Mock()
    coordinator.save_publication = Mock()
    pr = {"number": 2, "html_url": "https://example.test/pr/2", "head": {"sha": "d" * 40},
          "body": body(base, head)}
    coordinator.ensure_public_pr = Mock(return_value=pr)
    coordinator.wait_public_checks = Mock(return_value=pr)
    real_command = sync.command
    monkeypatch.setattr(sync, "command", lambda *args, **kwargs: b"" if args[:2] == ("git", "fetch")
                        else real_command(*args, **kwargs))
    merge_api = Mock(return_value={"merged": True, "sha": "e" * 40})
    monkeypatch.setattr(sync, "api", merge_api)
    coordinator.reconcile_main()
    coordinator.ensure_public_pr.assert_called_once_with(None, head, base, original, original, PRIVATE)
    merge_api.assert_called_once_with("repos/owner/public/pulls/2/merge", "PUT",
                                     {"sha": "d" * 40, "merge_method": "merge"})
    coordinator.save_publication.assert_called_once_with(head, "e" * 40, sync.publication_metadata(pr["body"]))


def test_private_state_initialization_update_and_duplicate_save(monkeypatch):
    first = {"version": 1, "source_head": "a" * 40, "public_head": "b" * 40,
             "commits": [{"source": "a" * 40, "public": "c" * 40, "exported": True}]}
    state_file = {"sha": "blob-one", "content": base64.b64encode(json.dumps(first).encode()).decode()}
    mock_api = Mock(side_effect=[None, {"sha": "tree"}, {"sha": "commit"}, {}, state_file,
                                {"content": {"sha": "blob-two"}}])
    monkeypatch.setattr(sync, "api", mock_api)
    coordinator = sync.Coordinator(CONFIG)
    coordinator.save_publication(first["source_head"], first["public_head"], {"commits": first["commits"]})
    assert mock_api.call_args_list[2].args[2]["parents"] == []
    assert mock_api.call_args_list[3].args[2]["ref"] == "refs/heads/codex/public-sync-state"
    coordinator.save_publication(first["source_head"], first["public_head"], {"commits": first["commits"]})
    assert mock_api.call_count == 5
    coordinator.save_publication("d" * 40, "e" * 40)
    payload = mock_api.call_args.args[2]
    assert payload["branch"] == sync.STATE_BRANCH and payload["sha"] == "blob-one"
    assert json.loads(base64.b64decode(payload["content"]))["source_head"] == "d" * 40
    assert coordinator.load_state()["public_head"] == "e" * 40


def test_only_a_missing_state_is_optional_not_an_authentication_failure(monkeypatch):
    monkeypatch.setattr(sync, "command", Mock(side_effect=RuntimeError("Not Found (HTTP 404)")))
    assert sync.api("state", missing_ok=True) is None
    monkeypatch.setattr(sync, "command", Mock(side_effect=RuntimeError("Forbidden (HTTP 403)")))
    with pytest.raises(RuntimeError, match="403"):
        sync.api("state", missing_ok=True)


def test_new_private_rule_cannot_hide_a_path_already_in_public_ancestry(repositories, tmp_path):
    source, public, base = repositories
    for repo in (source, public):
        (repo / "formerly-public.txt").write_text("already published\n")
        commit(repo, "Publish a file\n")
    base = sync.command("git", "rev-parse", "HEAD", cwd=source).decode().strip()
    (source / sync.CONFIG_PATH).write_text(json.dumps({"private_prefixes": [*PRIVATE, "formerly-public.txt"]}))
    commit(source, "Make the published file private\n")
    with pytest.raises(RuntimeError, match="clean its published history first"):
        export(repositories, tmp_path, base=base, prefixes=(*PRIVATE, "formerly-public.txt"))


def test_state_failure_after_public_merge_retries_without_a_second_merge(monkeypatch):
    base, head = "a" * 40, "b" * 40
    before, desired = {"public.txt": ("100644", "before")}, {"public.txt": ("100644", "after")}
    pr = {"number": 1, "head": {"sha": head}, "base": {"sha": base}}
    public_pr = {"number": 2, "state": "open", "html_url": "https://example.test/pr/2",
                 "head": {"sha": "c" * 40}, "body": body(base, head)}
    merged_pr = {**public_pr, "state": "closed", "merged_at": "2026-10-08", "merge_commit_sha": "d" * 40}
    monkeypatch.setattr(sync, "command", Mock())
    monkeypatch.setattr(subprocess, "run", Mock(return_value=Mock(returncode=0)))
    monkeypatch.setattr(sync, "prefixes_at_ref", lambda ref: PRIVATE)
    monkeypatch.setattr(sync, "local_tree", lambda ref: before if ref == base else desired)
    monkeypatch.setattr(sync, "public_history", lambda *args: [{"tree": desired}])
    monkeypatch.setattr(sync, "has_public_changes", Mock(return_value=True))
    merge_api = Mock(return_value={"merged": True, "sha": "d" * 40})
    monkeypatch.setattr(sync, "api", merge_api)
    coordinator = sync.Coordinator(CONFIG)
    coordinator.status = Mock()
    coordinator.verify_private_head = Mock()
    coordinator.merge_private = Mock()
    coordinator.matching_public_pr = Mock(side_effect=[None, merged_pr])
    coordinator.ensure_public_pr = Mock(return_value=public_pr)
    coordinator.wait_public_checks = Mock(return_value=public_pr)
    coordinator.remote_tree = Mock(side_effect=[before, desired, desired, desired, desired, desired])
    coordinator.save_publication = Mock(side_effect=[RuntimeError("state persistence failed"), None])
    with pytest.raises(RuntimeError, match="state persistence failed"):
        coordinator.process(pr)
    coordinator.merge_private.assert_not_called()
    coordinator.process(pr)
    coordinator.merge_private.assert_called_once_with(1, head)
    coordinator.ensure_public_pr.assert_called_once()
    coordinator.wait_public_checks.assert_called_once()
    merge_api.assert_called_once_with("repos/owner/public/pulls/2/merge", "PUT",
                                     {"sha": "c" * 40, "merge_method": "merge"})


def test_reverted_public_commits_are_published_from_a_ready_private_pr(repositories, tmp_path, monkeypatch):
    source, public, base = repositories
    actual = sync.local_tree("HEAD", public)
    (source / "public.txt").write_text("temporary\n")
    first = commit(source, "Try a feature\n")
    (source / "public.txt").write_text("baseline\n")
    head = commit(source, "Revert feature\n")
    mapping, _, target = export(repositories, tmp_path)
    assert [entry["source"] for entry in mapping if entry["exported"]] == [first, head]
    assert sync.local_tree("HEAD", target) == actual
    coordinator = sync.Coordinator(CONFIG)
    coordinator.remote_tree = Mock(return_value=actual)
    coordinator.status = Mock()
    coordinator.verify_private_head = Mock()
    coordinator.merge_private = Mock()
    coordinator.save_publication = Mock()
    coordinator.matching_public_pr = Mock(return_value=None)
    public_pr = {"number": 2, "html_url": "https://example.test/pr/2", "head": {"sha": "c" * 40},
                 "body": body(base, head, mapping)}
    coordinator.ensure_public_pr = Mock(return_value=public_pr)
    coordinator.wait_public_checks = Mock(return_value=public_pr)
    real_command = sync.command
    monkeypatch.setattr(sync, "command", lambda *args, **kwargs: b"" if args[:2] == ("git", "fetch")
                        else real_command(*args, **kwargs))
    monkeypatch.setattr(sync, "api", Mock(return_value={"merged": True, "sha": "d" * 40}))
    coordinator.process({"number": 1, "head": {"sha": head}, "base": {"sha": base}})
    coordinator.ensure_public_pr.assert_called_once_with(1, head, base, actual, actual, PRIVATE)
    coordinator.merge_private.assert_called_once_with(1, head)


def test_recovery_finds_a_merged_publication_when_the_cursor_write_was_lost(repositories, monkeypatch):
    source, _, base = repositories
    (source / "public.txt").write_text("published\n")
    head = commit(source, "Published public feature\n")
    desired = sync.public_tree(sync.local_tree(head), PRIVATE)
    mapping = [{"source": head, "public": "c" * 40, "exported": True}]
    published_pr = {"head": {"ref": "codex/public-sync-history-main-example"},
                    "body": body(base, head, mapping), "merged_at": "2026-10-08T15:00:00Z",
                    "merge_commit_sha": "d" * 40}
    api = Mock(return_value=[published_pr])
    monkeypatch.setattr(sync, "api", api)
    coordinator = sync.Coordinator(CONFIG)
    coordinator.remote_tree = Mock(return_value=desired)
    coordinator.private_main = Mock(return_value=head)
    coordinator.save_publication = Mock()
    coordinator.recover_publication(head, desired)
    coordinator.save_publication.assert_called_once_with(head, "d" * 40, sync.publication_metadata(published_pr["body"]))
    assert all(call.args[1:] == () for call in api.call_args_list)


def test_private_only_main_updates_leave_a_success_status_without_a_public_pr(repositories, monkeypatch):
    source, public, base = repositories
    (source / "applications/news_agent/private.txt").write_text("private-only update\n")
    head = commit(source, "Only private changes\n")
    actual = sync.local_tree("HEAD", public)
    state = {"source_head": base, "public_head": "c" * 40}
    coordinator = sync.Coordinator(CONFIG)
    coordinator.load_state = Mock(return_value=state)
    coordinator.public_main = Mock(return_value=state["public_head"])
    coordinator.remote_tree = Mock(return_value=actual)
    coordinator.private_main = Mock(return_value=head)
    coordinator.status = Mock()
    coordinator.ensure_public_pr = Mock(side_effect=AssertionError("must not publish"))
    real_command = sync.command
    monkeypatch.setattr(sync, "command", lambda *args, **kwargs: b"" if args[:2] == ("git", "fetch")
                        else real_command(*args, **kwargs))
    coordinator.reconcile_main()
    assert coordinator.status.call_args.args[1] == "success"
    coordinator.ensure_public_pr.assert_not_called()


@pytest.mark.parametrize("bad_mapping", [[{"source": "not-a-sha"}], "invalid", [{"source": "a" * 40,
    "public": "b" * 40, "exported": "true"}]])
def test_corrupted_commit_mapping_is_rejected(bad_mapping):
    with pytest.raises(RuntimeError, match="Invalid sync PR"):
        sync.publication_metadata(body("a" * 40, "b" * 40, bad_mapping))


def test_already_published_revert_is_not_exported_again_after_manual_private_merge(repositories, monkeypatch):
    source, public, base = repositories
    actual = sync.local_tree("HEAD", public)
    old_public = sync.command("git", "rev-parse", "HEAD", cwd=public).decode().strip()
    sync.command("git", "checkout", "-b", "feature", cwd=source)
    (source / "public.txt").write_text("temporary\n")
    first = commit(source, "Try public feature\n")
    (source / "public.txt").write_text("baseline\n")
    published_source = commit(source, "Revert public feature\n")
    sync.command("git", "checkout", "main", cwd=source)
    sync.command("git", "merge", "--no-ff", "feature", "-m", "Manual private merge", cwd=source)
    head = sync.command("git", "rev-parse", "HEAD", cwd=source).decode().strip()
    mapping = [{"source": first, "public": "c" * 40, "exported": True},
               {"source": published_source, "public": "d" * 40, "exported": True}]
    published_pr = {"head": {"ref": "codex/public-sync-history-1-example"}, "merged_at": "2026-10-08T15:00:00Z",
                    "merge_commit_sha": "e" * 40, "body": body(base, published_source, mapping)}
    coordinator = sync.Coordinator(CONFIG)
    coordinator._state_loaded = True
    coordinator._state = {"source_head": base, "public_head": old_public}
    coordinator.public_main = Mock(return_value="e" * 40)
    coordinator.private_main = Mock(return_value=head)
    coordinator.remote_tree = Mock(return_value=actual)
    coordinator.status = Mock()
    coordinator.ensure_public_pr = Mock(side_effect=AssertionError("must not republish reverted history"))

    def remember(source_head, public_head, metadata):
        coordinator._state = {"source_head": source_head, "public_head": public_head, "commits": metadata["commits"]}

    coordinator.save_publication = Mock(side_effect=remember)
    real_command = sync.command
    monkeypatch.setattr(sync, "command", lambda *args, **kwargs: b"" if args[:2] == ("git", "fetch")
                        else real_command(*args, **kwargs))
    monkeypatch.setattr(sync, "api", Mock(return_value=[published_pr]))
    coordinator.reconcile_main()
    coordinator.ensure_public_pr.assert_not_called()
    coordinator.save_publication.assert_called_once_with(published_source, "e" * 40,
                                                       sync.publication_metadata(published_pr["body"]))
    assert coordinator.status.call_args.args[1] == "success"


def test_private_only_branch_updated_from_newer_main_does_not_publish_old_snapshots(repositories, monkeypatch):
    source, _, initial = repositories
    sync.command("git", "branch", "private-feature", initial, cwd=source)
    (source / "new-main.txt").write_text("already published main change\n")
    base = commit(source, "New main public change\n")
    sync.command("git", "checkout", "private-feature", cwd=source)
    (source / "applications/news_agent/private.txt").write_text("private feature\n")
    commit(source, "Private feature\n")
    sync.command("git", "merge", "main", "-m", "Update private-only feature from main", cwd=source)
    head = sync.command("git", "rev-parse", "HEAD", cwd=source).decode().strip()
    history = sync.public_history(base, head, PRIVATE)
    assert history[0]["tree"] != sync.public_tree(sync.local_tree(base), PRIVATE)
    assert not sync.has_public_changes(history)
    coordinator = sync.Coordinator(CONFIG)
    coordinator.status = Mock()
    coordinator.verify_private_head = Mock()
    coordinator.merge_private = Mock()
    coordinator.remote_tree = Mock(side_effect=AssertionError("public API must not be needed"))
    real_command = sync.command
    monkeypatch.setattr(sync, "command", lambda *args, **kwargs: b"" if args[:2] == ("git", "fetch")
                        else real_command(*args, **kwargs))
    coordinator.process({"number": 1, "head": {"sha": head}, "base": {"sha": base}})
    coordinator.merge_private.assert_called_once_with(1, head)
