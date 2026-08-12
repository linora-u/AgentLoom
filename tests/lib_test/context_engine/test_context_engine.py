from __future__ import annotations

import contextvars
import threading

import pytest

from src.lib.context_engine import ContextEngine, ContextEngineConfig
from src.lib.context_engine.compressors import compress_content
from src.lib.context_engine.config import ContextStoreConfig
from src.lib.context_engine.models import ContentKind
from src.lib.context_engine.router import route_content
from src.lib.runtime import RuntimeHome


def test_context_engine_requires_explicit_thread_context_propagation(tmp_path):
    from src.lib.context_engine.runtime import (
        clear_current_context_engine,
        get_current_context_engine,
        set_current_context_engine,
    )

    engine = ContextEngine(tmp_path / "context_store")
    set_current_context_engine(engine)
    try:
        unpropagated = []
        thread = threading.Thread(
            target=lambda: unpropagated.append(get_current_context_engine())
        )
        thread.start()
        thread.join(timeout=5)

        propagated_context = contextvars.copy_context()
        propagated = []
        thread = threading.Thread(
            target=propagated_context.run,
            args=(lambda: propagated.append(get_current_context_engine()),),
        )
        thread.start()
        thread.join(timeout=5)

        assert unpropagated == [None]
        assert propagated == [engine]
    finally:
        clear_current_context_engine(engine)


def test_context_engine_stores_preview_and_retrieves_original(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(
            min_chars=10,
            preview_max_chars=120,
            store=ContextStoreConfig(max_entries=10),
        ),
    )
    original = "\n".join([f"line {i} with enough payload to be worth storing" for i in range(200)])

    preview = engine.compress_tool_result(original, tool_name="shell_tool", source="test")

    assert preview is not None
    assert preview.startswith("[ContextRef ctx_")
    ref = preview.split()[1]
    assert engine.retrieve(ref, offset=2, limit=3).splitlines()[0].startswith("line 2 ")
    assert engine.retrieve(ref, query="line 11").startswith("12: line 11 ")


def test_context_store_stays_on_original_task_inode_after_path_replacement(
    tmp_path,
) -> None:
    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="app", task_id="first", run_id="run-a")
    second = home.context(application_id="app", task_id="second", run_id="run-b")
    first.prepare_checkpoint()
    second.prepare_checkpoint()
    engine = ContextEngine(
        first.context_store_dir,
        config=ContextEngineConfig(min_chars=10, preview_max_chars=120),
    )
    detached = first.checkpoint_dir.parent / "first-detached"
    first.checkpoint_dir.rename(detached)
    first.checkpoint_dir.symlink_to(second.checkpoint_dir, target_is_directory=True)
    original = "\n".join(f"FIRST secret line {index}" for index in range(100))

    preview = engine.compress_tool_result(original, tool_name="shell_tool")

    assert preview is not None
    assert list((second.checkpoint_dir / "context_store").glob("**/ctx_*.json")) == []
    assert len(list((detached / "context_store" / "entries").glob("ctx_*.json"))) == 1


def test_context_router_detects_core_content_kinds():
    assert route_content('{"items": [1, 2]}') == ContentKind.JSON
    assert route_content("diff --git a/a.py b/a.py\n@@ -1 +1 @@") == ContentKind.DIFF
    assert route_content("src/a.py:10:def hello():") == ContentKind.SEARCH
    assert route_content("Traceback\nValueError: boom") == ContentKind.LOG
    assert route_content("2026-06-21T10:03:01Z ERROR case=log failed") == ContentKind.LOG
    assert route_content("def hello():\n    return 1", tool_name="read_file") == ContentKind.CODE
    assert route_content("plain text") == ContentKind.TEXT
    assert route_content("plain text", tool_name="build_output") == ContentKind.LOG


def test_json_smart_crusher_lite_preserves_error_items(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=500),
    )
    original = "[" + ",".join(
        ['{"status": "ok", "idx": %d, "payload": "%s"}' % (i, "x" * 80) for i in range(80)]
        + ['{"status": "error", "message": "failed needle"}']
    ) + "]"

    preview = engine.compress_tool_result(original, tool_name="api_tool", source="json")

    assert preview is not None
    assert "failed needle" in preview
    assert '"dropped_items"' in preview


def test_context_store_ttl_expires_entries(monkeypatch, tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(
            min_chars=10,
            preview_max_chars=120,
            store=ContextStoreConfig(ttl_seconds=1),
        ),
    )
    preview = engine.compress_tool_result("ttl payload\n" * 80, tool_name="shell_tool", source="ttl")
    assert preview is not None
    ref = preview.split()[1]
    entry = engine.get_entry(ref)
    assert entry is not None

    monkeypatch.setattr("src.lib.context_engine.store.time.time", lambda: entry.created_at + 2)

    assert engine.retrieve(ref) is None


def test_context_store_evicts_oldest_entry_when_max_entries_exceeded(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(
            min_chars=10,
            preview_max_chars=120,
            store=ContextStoreConfig(max_entries=2),
        ),
    )
    refs = []
    for idx in range(3):
        preview = engine.compress_tool_result(
            (f"payload {idx}\n" * 80),
            tool_name="shell_tool",
            source=f"evict-{idx}",
        )
        assert preview is not None
        refs.append(preview.split()[1])

    assert refs[0] not in engine.store.refs()
    assert refs[1:] == engine.store.refs()


def test_log_compressor_preserves_error_traceback_and_tail():
    lines = [f"INFO warmup row={idx:03d}" for idx in range(80)]
    lines.extend(
        [
            "ERROR validation failed",
            "Traceback (most recent call last):",
            "ValueError: LOG-NEEDLE",
        ]
    )
    lines.extend(f"INFO tail row={idx:03d}" for idx in range(80, 140))

    result = compress_content("\n".join(lines), ContentKind.LOG, preview_max_chars=1600)

    assert result.strategy == "log_errors_tail"
    assert "ERROR validation failed" in result.preview
    assert "ValueError: LOG-NEEDLE" in result.preview
    assert "INFO tail row=139" in result.preview


@pytest.mark.parametrize("limit", [1, 20, 120, 500, 3000])
def test_compressor_preview_never_exceeds_positive_limit(limit):
    result = compress_content("payload\n" * 2000, ContentKind.TEXT, preview_max_chars=limit)

    assert len(result.preview) <= limit


def test_log_compressor_prioritizes_middle_error_under_tight_limit():
    lines = [f"INFO head row={idx:03d} {'x' * 20}" for idx in range(70)]
    lines.append("ERROR UNIQUE-MIDDLE-FAILURE")
    lines.extend(f"INFO tail row={idx:03d} {'y' * 20}" for idx in range(70, 140))

    result = compress_content("\n".join(lines), ContentKind.LOG, preview_max_chars=320)

    assert len(result.preview) <= 320
    assert "UNIQUE-MIDDLE-FAILURE" in result.preview


def test_json_compressor_prioritizes_middle_error_under_tight_limit():
    items = [
        {"status": "ok", "idx": idx, "payload": "x" * 60}
        for idx in range(80)
    ]
    items[40] = {"status": "error", "message": "UNIQUE-JSON-MIDDLE-FAILURE"}

    result = compress_content(
        __import__("json").dumps(items),
        ContentKind.JSON,
        preview_max_chars=400,
    )

    assert len(result.preview) <= 400
    assert "UNIQUE-JSON-MIDDLE-FAILURE" in result.preview


def test_diff_compressor_preserves_file_hunks_and_changed_lines():
    diff = "\n".join(
        [
            "diff --git a/a.py b/a.py",
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1,3 +1,3 @@",
            "-old_value = 1",
            "+new_value = 2",
            " context = True",
        ]
        + [f" unchanged filler {idx}" for idx in range(200)]
    )

    result = compress_content(diff, ContentKind.DIFF, preview_max_chars=400)

    assert result.strategy == "diff_headers_changes"
    assert "diff --git a/a.py b/a.py" in result.preview
    assert "@@ -1,3 +1,3 @@" in result.preview
    assert "-old_value = 1" in result.preview
    assert "+new_value = 2" in result.preview


def test_search_compressor_preserves_match_shape_and_tail():
    search_output = "\n".join(
        [f"src/pkg/mod_{idx}.py:{idx}: ordinary match" for idx in range(60)]
        + ["src/pkg/target.py:777: SEARCH-NEEDLE failed"]
        + [f"src/pkg/tail_{idx}.py:{idx}: trailing match" for idx in range(60, 120)]
    )

    result = compress_content(search_output, ContentKind.SEARCH, preview_max_chars=1600)

    assert result.strategy == "search_first_last_errors"
    assert "[Search summary:" in result.preview
    assert "src/pkg/mod_0.py:0: ordinary match" in result.preview
    assert "SEARCH-NEEDLE failed" in result.preview
    assert "src/pkg/tail_119.py:119: trailing match" in result.preview
