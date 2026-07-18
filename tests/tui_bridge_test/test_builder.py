from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.tui_bridge.builder as builder_module
from src.tui_bridge.builder import BuilderService, DraftConflictError

VALID_AGENT_YAML = """\
name: report_agent
description: Build a concise report.
model_type: powerful
tool_call_type: tool_call
workflow: |
  Ask for the report topic, collect the required facts, and return a concise report.
"""
REPORT_AGENT_PATH = "applications/reports/workflows/report_agent.yaml"


@pytest.fixture(autouse=True)
def _configured_model_catalog(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )


class _StubChatAgent:
    def __init__(self, replies: list[str]):
        self._replies = replies
        self.histories: list[list[dict[str, str]]] = []

    def run(self, *, history, model_type, tools, on_event):
        del tools, on_event
        self.histories.append([dict(item) for item in history])
        return SimpleNamespace(
            assistant=self._replies.pop(0),
            model_type=model_type or "powerful",
        )


def _stubbed_service(
    project_root: Path,
    replies: list[str] | None = None,
) -> tuple[BuilderService, _StubChatAgent]:
    service = BuilderService(project_root)
    chat_agent = _StubChatAgent(list(replies or ["Draft ready."]))
    service._chat_agent = chat_agent
    return service, chat_agent


def _stage_yaml(
    service: BuilderService,
    path: str = REPORT_AGENT_PATH,
    content: str = VALID_AGENT_YAML,
    *,
    session_id: str = "builder-1",
) -> dict[str, object]:
    tool = builder_module._StageAgentYamlTool(
        service._project_root,
        service._draft(session_id),
    )
    return json.loads(tool.forward(path, content))


def _validate_draft(
    service: BuilderService,
    *,
    session_id: str = "builder-1",
) -> dict[str, object]:
    tool = builder_module._ValidateAgentDraftTool(
        service._project_root,
        service._draft(session_id),
    )
    return json.loads(tool.forward())


def _staged_service(
    project_root: Path,
    files: dict[str, str] | None = None,
) -> BuilderService:
    service = BuilderService(project_root)
    for path, content in (files or {REPORT_AGENT_PATH: VALID_AGENT_YAML}).items():
        _stage_yaml(service, path, content)
    return service


def test_draft_is_written_only_after_explicit_apply_with_matching_revision(tmp_path: Path) -> None:
    service = _staged_service(tmp_path)

    with pytest.raises(DraftConflictError, match="revision"):
        service.apply_draft(session_id="builder-1", expected_revision=0)

    result = service.apply_draft(session_id="builder-1", expected_revision=1)

    target = tmp_path / "applications/reports/workflows/report_agent.yaml"
    assert target.read_text(encoding="utf-8") == VALID_AGENT_YAML
    assert result == {
        "applied": True,
        "revision": 1,
        "files": ["applications/reports/workflows/report_agent.yaml"],
    }


def test_apply_preserves_existing_target_permissions(tmp_path: Path) -> None:
    target = tmp_path / "applications/reports/workflows/report_agent.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)
    service = _staged_service(tmp_path)

    service.apply_draft(session_id="builder-1", expected_revision=1)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_apply_rejects_existing_target_changed_after_first_stage(
    tmp_path: Path,
) -> None:
    target_path = "applications/reports/workflows/report_agent.yaml"
    target = tmp_path / target_path
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    original_stat = target.stat()
    service = _staged_service(tmp_path)
    target.write_text("new\n", encoding="utf-8")
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    _stage_yaml(service, target_path, VALID_AGENT_YAML.replace("concise", "detailed"))

    with pytest.raises(DraftConflictError, match="changed since it was first staged"):
        service.apply_draft(session_id="builder-1", expected_revision=2)

    assert target.read_text(encoding="utf-8") == "new\n"
    assert list(target.parent.glob(".*.agentloom-*")) == []


def test_apply_rejects_target_created_after_first_stage(tmp_path: Path) -> None:
    service = _staged_service(tmp_path)
    target = tmp_path / "applications/reports/workflows/report_agent.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("concurrent create\n", encoding="utf-8")

    with pytest.raises(DraftConflictError, match="changed since it was first staged"):
        service.apply_draft(session_id="builder-1", expected_revision=1)

    assert target.read_text(encoding="utf-8") == "concurrent create\n"
    assert list(target.parent.glob(".*.agentloom-*")) == []


def test_successful_apply_consumes_the_backend_draft(tmp_path: Path) -> None:
    service = _staged_service(tmp_path)

    service.apply_draft(session_id="builder-1", expected_revision=1)

    target = tmp_path / "applications/reports/workflows/report_agent.yaml"
    target.write_text("external edit\n", encoding="utf-8")
    draft = service.get_draft("builder-1")

    assert draft == {
        "revision": 1,
        "valid": False,
        "errors": ["No Agent YAML files are staged"],
        "files": [],
    }
    assert target.read_text(encoding="utf-8") == "external edit\n"


def test_apply_removes_temporary_file_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _staged_service(tmp_path)

    def fail_link(
        _source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        _target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        del src_dir_fd, dst_dir_fd, follow_symlinks
        raise OSError("disk failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(OSError, match="disk failure"):
        service.apply_draft(session_id="builder-1", expected_revision=1)

    target_dir = tmp_path / "applications/reports/workflows"
    assert not (target_dir / "report_agent.yaml").exists()
    assert list(target_dir.glob(".report_agent.yaml.agentloom-*.tmp")) == []


def test_apply_rejects_parent_replaced_after_it_was_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflows = tmp_path / "applications/reports/workflows"
    workflows.mkdir(parents=True)
    displaced_workflows = workflows.with_name("workflows-original")
    outside = tmp_path / "outside"
    outside.mkdir()
    service = _staged_service(tmp_path)
    real_open = os.open
    swapped = False

    def swap_parent_before_temporary_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and os.fsdecode(path).endswith(".tmp"):
            workflows.rename(displaced_workflows)
            workflows.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_parent_before_temporary_open)

    with pytest.raises(DraftConflictError, match="parent changed before commit"):
        service.apply_draft(session_id="builder-1", expected_revision=1)

    assert swapped is True
    assert not (displaced_workflows / "report_agent.yaml").exists()
    assert list(outside.iterdir()) == []
    assert list(displaced_workflows.glob(".*.agentloom-*")) == []


def test_multi_file_apply_rolls_back_every_target_when_one_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    first = tmp_path / first_path
    second = tmp_path / second_path
    first.parent.mkdir(parents=True)
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    real_replace = os.replace

    def fail_second_temporary(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if Path(source).suffix == ".tmp" and os.fsdecode(target) == second.name:
            raise OSError("second replace failed")
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", fail_second_temporary)

    with pytest.raises(OSError, match="second replace failed"):
        service.apply_draft(session_id="builder-1", expected_revision=2)

    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "old second\n"
    assert list(first.parent.glob(".*.agentloom-*")) == []


def test_apply_preflights_every_original_fingerprint_before_committing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    first = tmp_path / first_path
    second = tmp_path / second_path
    first.parent.mkdir(parents=True)
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    original_stat = first.stat()
    real_open = os.open
    changed = False

    def change_first_before_second_prepare(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal changed
        name = os.fsdecode(path)
        if not changed and name.startswith(".second.yaml.agentloom-") and name.endswith(".tmp"):
            first.write_text("new first\n", encoding="utf-8")
            os.utime(first, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            changed = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", change_first_before_second_prepare)

    with pytest.raises(DraftConflictError, match="changed before commit"):
        service.apply_draft(session_id="builder-1", expected_revision=2)

    assert changed is True
    assert first.read_text(encoding="utf-8") == "new first\n"
    assert second.read_text(encoding="utf-8") == "old second\n"
    assert list(first.parent.glob(".*.agentloom-*")) == []


def test_apply_rechecks_each_target_immediately_before_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    first = tmp_path / first_path
    second = tmp_path / second_path
    first.parent.mkdir(parents=True)
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    second_stat = second.stat()
    real_replace = os.replace
    changed = False

    def change_second_after_first_commit(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal changed
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if Path(source).suffix == ".tmp" and os.fsdecode(target) == first.name:
            second.write_text("new second\n", encoding="utf-8")
            os.utime(second, ns=(second_stat.st_atime_ns, second_stat.st_mtime_ns))
            changed = True

    monkeypatch.setattr(os, "replace", change_second_after_first_commit)

    with pytest.raises(DraftConflictError, match="changed before commit"):
        service.apply_draft(session_id="builder-1", expected_revision=2)

    assert changed is True
    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "new second\n"
    assert list(first.parent.glob(".*.agentloom-*")) == []


def test_apply_never_clobbers_target_created_after_absence_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _staged_service(tmp_path)
    target = tmp_path / "applications/reports/workflows/report_agent.yaml"
    real_link = os.link
    raced = False

    def create_target_before_no_clobber_link(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal raced
        if not raced and os.fsdecode(destination) == target.name:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, b"concurrent create\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            raced = True
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", create_target_before_no_clobber_link)

    with pytest.raises(DraftConflictError, match="created before commit"):
        service.apply_draft(session_id="builder-1", expected_revision=1)

    assert raced is True
    assert target.read_text(encoding="utf-8") == "concurrent create\n"
    assert list(target.parent.glob(".*.agentloom-*")) == []


def test_parent_replacement_conflict_rolls_back_in_opened_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    workflows = tmp_path / "applications/reports/workflows"
    displaced_workflows = workflows.with_name("workflows-original")
    outside = tmp_path / "outside"
    workflows.mkdir(parents=True)
    outside.mkdir()
    first = workflows / "first.yaml"
    second = workflows / "second.yaml"
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    real_replace = os.replace
    temporary_replaces = 0

    def swap_parent_after_first_apply(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal temporary_replaces
        if Path(source).suffix == ".tmp":
            temporary_replaces += 1
            if temporary_replaces == 1:
                workflows.rename(displaced_workflows)
                workflows.symlink_to(outside, target_is_directory=True)
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", swap_parent_after_first_apply)

    with pytest.raises(DraftConflictError, match="parent changed before commit"):
        service.apply_draft(session_id="builder-1", expected_revision=2)

    assert temporary_replaces == 1
    assert (displaced_workflows / "first.yaml").read_text(encoding="utf-8") == "old first\n"
    assert (displaced_workflows / "second.yaml").read_text(encoding="utf-8") == "old second\n"
    assert list(displaced_workflows.glob(".*.agentloom-*")) == []
    assert list(outside.iterdir()) == []


def test_single_file_apply_revalidates_parent_after_the_final_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows = tmp_path / "applications/reports/workflows"
    displaced_workflows = workflows.with_name("workflows-original")
    service = _staged_service(tmp_path)
    target_name = "report_agent.yaml"
    real_link = os.link
    swapped = False

    def swap_parent_after_link(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if not swapped and os.fsdecode(destination) == target_name:
            workflows.rename(displaced_workflows)
            workflows.mkdir()
            swapped = True

    monkeypatch.setattr(os, "link", swap_parent_after_link)

    with pytest.raises(DraftConflictError, match="parent changed before commit"):
        service.apply_draft(session_id="builder-1", expected_revision=1)

    assert swapped is True
    assert not (workflows / target_name).exists()
    assert not (displaced_workflows / target_name).exists()
    assert list(displaced_workflows.glob(".*.agentloom-*")) == []


def test_apply_fsyncs_parent_directory_after_commit_and_rollback_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    first = tmp_path / first_path
    second = tmp_path / second_path
    first.parent.mkdir(parents=True)
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    real_replace = os.replace
    real_fsync = os.fsync
    events: list[str] = []

    def record_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        source_name = os.fsdecode(source)
        target_name = os.fsdecode(target)
        if Path(source_name).suffix == ".tmp" and target_name == second.name:
            raise OSError("second replace failed")
        events.append(f"replace:{Path(source_name).suffix}->{target_name}")
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def record_fsync(descriptor: int) -> None:
        events.append("fsync:directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "fsync:file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", record_replace)
    monkeypatch.setattr(os, "fsync", record_fsync)

    with pytest.raises(OSError, match="second replace failed"):
        service.apply_draft(session_id="builder-1", expected_revision=2)

    first_commit = events.index("replace:.tmp->first.yaml")
    first_rollback = events.index("replace:.bak->first.yaml")
    assert events[first_commit + 1] == "fsync:directory"
    assert events[first_rollback + 1] == "fsync:directory"
    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "old second\n"


def test_prepare_failure_removes_partial_temporary_and_backup_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "applications/reports/workflows/report_agent.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("old content\n", encoding="utf-8")
    service = _staged_service(tmp_path)

    def fail_after_partial_backup(directory_fd: int, _source_name: str, backup_name: str) -> None:
        descriptor = os.open(
            backup_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o666,
            dir_fd=directory_fd,
        )
        try:
            os.write(descriptor, b"partial backup")
        finally:
            os.close(descriptor)
        raise OSError("backup write failed")

    monkeypatch.setattr(builder_module, "_backup_file", fail_after_partial_backup)

    with pytest.raises(OSError, match="backup write failed"):
        service.apply_draft(session_id="builder-1", expected_revision=1)

    assert target.read_text(encoding="utf-8") == "old content\n"
    assert list(target.parent.glob(".report_agent.yaml.agentloom-*.tmp")) == []
    assert list(target.parent.glob(".report_agent.yaml.agentloom-*.bak")) == []


def test_incomplete_rollback_preserves_recovery_backup_and_reports_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    first = tmp_path / first_path
    second = tmp_path / second_path
    first.parent.mkdir(parents=True)
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    real_replace = os.replace

    def fail_apply_then_rollback(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        source_path = Path(source)
        target_name = os.fsdecode(target)
        if source_path.suffix == ".tmp" and target_name == second.name:
            raise OSError("second apply failed")
        if source_path.suffix == ".bak" and target_name == first.name:
            raise OSError("first rollback failed")
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", fail_apply_then_rollback)

    with pytest.raises(RuntimeError, match="rollback was incomplete") as captured:
        service.apply_draft(session_id="builder-1", expected_revision=2)

    recovery_backups = list(first.parent.glob(".first.yaml.agentloom-*.bak"))
    assert len(recovery_backups) == 1
    assert recovery_backups[0].read_text(encoding="utf-8") == "old first\n"
    assert str(recovery_backups[0]) in str(captured.value)
    assert first.read_text(encoding="utf-8") == VALID_AGENT_YAML
    assert second.read_text(encoding="utf-8") == "old second\n"
    assert list(first.parent.glob(".*.agentloom-*.tmp")) == []


def test_rollback_does_not_overwrite_target_changed_after_transaction_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = "applications/reports/workflows/first.yaml"
    second_path = "applications/reports/workflows/second.yaml"
    first = tmp_path / first_path
    second = tmp_path / second_path
    first.parent.mkdir(parents=True)
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    service = _staged_service(
        tmp_path,
        {
            first_path: VALID_AGENT_YAML,
            second_path: VALID_AGENT_YAML.replace("report_agent", "second_agent"),
        },
    )
    real_replace = os.replace
    concurrent_content = "X" * len(VALID_AGENT_YAML.encode())
    changed_after_commit = False

    def change_first_after_commit_then_fail_second(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal changed_after_commit
        source_path = Path(source)
        target_name = os.fsdecode(target)
        if source_path.suffix == ".tmp" and target_name == second.name:
            raise OSError("second apply failed")
        real_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if source_path.suffix == ".tmp" and target_name == first.name:
            committed_stat = os.stat(target, dir_fd=dst_dir_fd, follow_symlinks=False)
            descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC, dir_fd=dst_dir_fd)
            try:
                os.write(descriptor, concurrent_content.encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.utime(
                target,
                ns=(committed_stat.st_atime_ns, committed_stat.st_mtime_ns),
                dir_fd=dst_dir_fd,
                follow_symlinks=False,
            )
            changed_after_commit = True

    monkeypatch.setattr(os, "replace", change_first_after_commit_then_fail_second)

    with pytest.raises(RuntimeError, match="changed after commit") as captured:
        service.apply_draft(session_id="builder-1", expected_revision=2)

    recovery_backups = list(first.parent.glob(".first.yaml.agentloom-*.bak"))
    assert changed_after_commit is True
    assert first.read_text(encoding="utf-8") == concurrent_content
    assert second.read_text(encoding="utf-8") == "old second\n"
    assert len(recovery_backups) == 1
    assert recovery_backups[0].read_text(encoding="utf-8") == "old first\n"
    assert str(recovery_backups[0]) in str(captured.value)
    assert list(first.parent.glob(".*.agentloom-*.tmp")) == []


def test_stage_tool_rejects_paths_outside_application_workflows(tmp_path: Path) -> None:
    service = BuilderService(tmp_path)

    with pytest.raises(ValueError, match="applications/.+/workflows"):
        _stage_yaml(service, "../../config/llm.yaml", VALID_AGENT_YAML)


def test_stage_tool_rejects_symlinked_application_parent(tmp_path: Path) -> None:
    applications = tmp_path / "applications"
    applications.mkdir()
    (applications / "evil").symlink_to(tmp_path, target_is_directory=True)
    service = BuilderService(tmp_path)

    with pytest.raises(ValueError, match="symlink"):
        _stage_yaml(
            service,
            "applications/evil/workflows/escaped.yaml",
            VALID_AGENT_YAML,
        )
    assert not (tmp_path / "workflows/escaped.yaml").exists()


def test_stage_tool_rejects_existing_file_symlink(tmp_path: Path) -> None:
    workflows = tmp_path / "applications/reports/workflows"
    workflows.mkdir(parents=True)
    real = workflows / "real.yaml"
    real.write_text("do not overwrite\n", encoding="utf-8")
    link = workflows / "linked.yaml"
    link.symlink_to(real)
    service = BuilderService(tmp_path)

    with pytest.raises(ValueError, match="symlink"):
        _stage_yaml(
            service,
            "applications/reports/workflows/linked.yaml",
            VALID_AGENT_YAML,
        )
    assert real.read_text(encoding="utf-8") == "do not overwrite\n"


def test_inspect_tool_skips_yaml_symlinks_outside_the_project(tmp_path: Path) -> None:
    workflows = tmp_path / "applications/reports/workflows"
    workflows.mkdir(parents=True)
    (workflows / "real.yaml").write_text(VALID_AGENT_YAML, encoding="utf-8")
    secret = tmp_path.parent / f"{tmp_path.name}-secret.yaml"
    secret.write_text("api_key: must-not-leak\n", encoding="utf-8")
    (workflows / "linked.yaml").symlink_to(secret)
    tool = builder_module._InspectAgentSystemTool(tmp_path.resolve())

    result = json.loads(tool.forward("reports"))

    assert [item["path"] for item in result["files"]] == ["applications/reports/workflows/real.yaml"]
    assert "must-not-leak" not in json.dumps(result)


def test_validation_reports_missing_required_agent_fields(tmp_path: Path) -> None:
    service = BuilderService(tmp_path)
    _stage_yaml(
        service,
        "applications/reports/workflows/broken.yaml",
        "name: broken\n",
    )

    result = _validate_draft(service)

    assert result["valid"] is False
    assert "description" in result["errors"][0]
    assert "workflow" in result["errors"][0]


def test_validation_rejects_numeric_description_before_runtime(tmp_path: Path) -> None:
    service = BuilderService(tmp_path)
    _stage_yaml(
        service,
        "applications/reports/workflows/broken.yaml",
        "name: broken\ndescription: 123\nworkflow: do the task\n",
    )

    result = _validate_draft(service)

    errors = "\n".join(result["errors"])
    assert result["valid"] is False
    assert "description must be a non-empty string" in errors


def test_validation_rejects_invalid_execution_environment_before_runtime(tmp_path: Path) -> None:
    service = BuilderService(tmp_path)
    _stage_yaml(
        service,
        "applications/reports/workflows/broken.yaml",
        VALID_AGENT_YAML + "execution_env: []\n",
    )

    result = _validate_draft(service)

    errors = "\n".join(result["errors"])
    assert result["valid"] is False
    assert "execution_env must be a dictionary" in errors


def test_validation_rejects_runtime_invalid_structure_model_and_worker_reference(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/llm.yaml").write_text(
        "model:\n  default_model_type: powerful\n  powerful:\n    model: openai/test\n",
        encoding="utf-8",
    )

    service = BuilderService(tmp_path)
    _stage_yaml(
        service,
        "applications/reports/workflows/broken.yaml",
        VALID_AGENT_YAML
        + "tool_call_type: unsupported\n"
        + "model_type: missing-model\n"
        + "worker_agents:\n  - path: missing_worker.yaml\n",
    )
    result = _validate_draft(service)

    errors = "\n".join(result["errors"])
    assert result["valid"] is False
    assert "tool_call_type" in errors
    assert "missing-model" in errors
    assert "missing_worker.yaml" in errors


def test_draft_validation_rejects_an_invalid_existing_worker_definition(tmp_path: Path) -> None:
    worker = tmp_path / "applications/reports/workflows/worker_agents/broken_worker.yaml"
    worker.parent.mkdir(parents=True)
    worker.write_text(
        """\
name: broken_worker
description: invalid worker
workflow: do the task
max_steps: true
agent_function_schema:
  description: Handle one task.
  inputs:
    task:
      description: Task to handle.
  output:
    description: Worker result.
""",
        encoding="utf-8",
    )

    service = BuilderService(tmp_path)
    _stage_yaml(
        service,
        "applications/reports/workflows/supervisor.yaml",
        """\
name: supervisor
description: delegate
workflow: delegate the task
worker_agents:
  - path: broken_worker.yaml
""",
    )

    result = _validate_draft(service)

    errors = "\n".join(result["errors"])
    assert result["valid"] is False
    assert "broken_worker.yaml" in errors
    assert "max_steps must be a positive integer" in errors


@pytest.mark.parametrize(
    ("worker_schema", "expected_error"),
    [
        ("", "agent_function_schema is required"),
        (
            """\
agent_function_schema:
  description: Handle one task.
  inputs: []
  output:
    description: Worker result.
""",
            "agent_function_schema.inputs",
        ),
    ],
)
def test_draft_validation_rejects_invalid_staged_referenced_worker_definition(
    tmp_path: Path,
    worker_schema: str,
    expected_error: str,
) -> None:
    service = BuilderService(tmp_path)
    _stage_yaml(
        service,
        "applications/reports/workflows/supervisor.yaml",
        """\
name: supervisor
description: delegate
workflow: delegate the task
worker_agents:
  - path: staged_worker.yaml
""",
    )
    _stage_yaml(
        service,
        "applications/reports/workflows/worker_agents/staged_worker.yaml",
        """\
name: staged_worker
description: staged worker
workflow: do the task
"""
        + worker_schema,
    )

    result = _validate_draft(service)

    errors = "\n".join(result["errors"])
    assert result["valid"] is False
    assert "staged_worker.yaml" in errors
    assert expected_error in errors


def test_builder_keeps_a_bounded_conversation_transcript(tmp_path: Path) -> None:
    replies = ["What should it produce?", "Draft ready."]
    service, chat_agent = _stubbed_service(tmp_path, replies=replies)

    first = service.send(session_id="builder-1", message="I need an agent")
    second = service.send(session_id="builder-1", message="Create a daily report")

    assert first["assistant"] == "What should it produce?"
    assert second["assistant"] == "Draft ready."
    assert [item["content"] for item in chat_agent.histories[1]] == [
        "I need an agent",
        "What should it produce?",
        "Create a daily report",
    ]
    assert len(service.history("builder-1")) == 4


def test_builder_rejects_an_oversized_user_message_before_calling_the_model(tmp_path: Path) -> None:
    service, chat_agent = _stubbed_service(tmp_path)

    with pytest.raises(ValueError, match="32,000 characters"):
        service.send(session_id="builder-1", message="x" * 32_001)

    assert chat_agent.histories == []
    assert service.history("builder-1") == []


def test_builder_bounds_assistant_output_and_total_transcript_size(tmp_path: Path) -> None:
    service, _ = _stubbed_service(tmp_path, replies=["a" * 40_000] * 10)

    for index in range(10):
        result = service.send(session_id="builder-1", message=f"turn-{index}-" + "u" * 1_000)

    history = service.history("builder-1")
    assert len(result["assistant"]) <= 32_000
    assert result["assistant"].endswith("… [truncated]")
    assert len(history) <= 16
    assert sum(len(item["content"]) for item in history) <= 64_000


def test_builder_result_is_json_serializable(tmp_path: Path) -> None:
    service, _ = _stubbed_service(tmp_path)
    _stage_yaml(service)

    result = service.send(session_id="builder-1", message="Create a report agent")

    assert json.loads(json.dumps(result))["draft"]["valid"] is True
