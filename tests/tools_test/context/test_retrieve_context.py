from __future__ import annotations

from src.lib.context_engine import ContextEngine, ContextEngineConfig
from src.lib.context_engine.runtime import clear_current_context_engine, set_current_context_engine
from src.tools.context.retrieve_context import loom_retrieve_context


def test_loom_retrieve_context_returns_query_matches_from_active_store(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=120),
    )
    set_current_context_engine(engine)
    try:
        original = "\n".join(
            [f"ordinary line {idx} with enough filler to require context storage" for idx in range(120)]
            + ["TARGET line carries RETRIEVE-NEEDLE"]
            + [f"tail line {idx} with enough filler to require context storage" for idx in range(120, 240)]
        )
        preview = engine.compress_tool_result(original, tool_name="shell_tool", source="test")
        assert preview is not None
        ref = preview.split()[1]

        result = loom_retrieve_context(ref=ref, query="RETRIEVE-NEEDLE", offset=0, limit=5)

        assert "retrieved kind=text" in result
        assert "TARGET line carries RETRIEVE-NEEDLE" in result
    finally:
        clear_current_context_engine(engine)


def test_loom_retrieve_context_paginates_full_original_content(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=120),
    )
    set_current_context_engine(engine)
    try:
        preview = engine.compress_tool_result(
            "\n".join(f"line-{idx} with enough filler to require context storage" for idx in range(300)),
            tool_name="shell_tool",
            source="test",
        )
        assert preview is not None
        ref = preview.split()[1]

        result = loom_retrieve_context(ref=ref, offset=12, limit=3)

        assert "line-12 with enough filler to require context storage" in result
        assert "line-13 with enough filler to require context storage" in result
        assert "line-14 with enough filler to require context storage" in result
        assert "line-11" not in result
        assert "line-15" not in result
    finally:
        clear_current_context_engine(engine)


def test_loom_retrieve_context_requires_active_task_scoped_store():
    clear_current_context_engine()

    assert (
        loom_retrieve_context(ref="ctx_0123456789abcdef")
        == "No active ContextEngine; context refs require an active task-scoped store."
    )
