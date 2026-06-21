from __future__ import annotations

from smolagents.models import ChatMessage, MessageRole

import src.lib.smolagents.memory.context_compression as compression_module
from src.lib.context_engine import ContextEngine, ContextEngineConfig
from src.lib.context_engine.config import ContextSafetyConfig
from src.lib.context_engine.runtime import clear_current_context_engine, set_current_context_engine
from src.lib.smolagents.memory.context_compression import (
    ConversationHistoryManager,
    InternalChatMessage,
    _extract_content_text,
)


def _msg(role, text):
    return ChatMessage(role=role, content=[{"type": "text", "text": text}])


def test_context_engine_layer_replaces_large_tool_response_with_ref(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=160),
    )
    set_current_context_engine(engine)
    try:
        messages = [
            InternalChatMessage.from_chat_message(_msg(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}")),
            InternalChatMessage.from_chat_message(_msg(MessageRole.TOOL_RESPONSE, "important output\n" * 50)),
        ]

        new_messages, saved = compression_module._apply_context_engine_compression(messages)

        assert saved > 0
        text = _extract_content_text(new_messages[1].message.content)
        assert text.startswith("[ContextRef ctx_")
        ref = text.split()[1]
        assert "important output" in engine.retrieve(ref, offset=0, limit=1)
    finally:
        clear_current_context_engine(engine)


def test_context_engine_layer_is_idempotent_for_existing_ref(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=160),
    )
    set_current_context_engine(engine)
    try:
        existing = '[ContextRef ctx_0123456789abcdef kind=text source=shell_tool original_chars=10 preview_chars=5]'
        messages = [
            InternalChatMessage.from_chat_message(_msg(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}")),
            InternalChatMessage.from_chat_message(_msg(MessageRole.TOOL_RESPONSE, existing)),
        ]

        new_messages, saved = compression_module._apply_context_engine_compression(messages)

        assert saved == 0
        assert new_messages[1] is messages[1]
    finally:
        clear_current_context_engine(engine)


def test_conversation_manager_prefers_context_engine_before_hard_truncation(monkeypatch, tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=160),
    )
    set_current_context_engine(engine)
    calls = {"n": 0}

    def count_tokens(messages, _model_id):
        calls["n"] += 1
        text = "\n".join(_extract_content_text(msg.content) for msg in messages)
        return 9999 if "important output" in text and "ContextRef" not in text else 1

    monkeypatch.setattr(compression_module, "_count_tokens", count_tokens)
    try:
        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(
            [
                _msg(MessageRole.SYSTEM, "system"),
                _msg(MessageRole.USER, "task"),
                _msg(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}"),
                _msg(MessageRole.TOOL_RESPONSE, "important output\n" * 50),
            ]
        )

        result = manager.get_compressed_messages("dummy")

        visible = "\n".join(_extract_content_text(msg.content) for msg in result)
        assert "ContextRef ctx_" in visible
        assert "Truncated" not in visible
        assert calls["n"] >= 2
    finally:
        clear_current_context_engine(engine)


def test_compressed_history_round_trips_original_tool_outputs(monkeypatch, tmp_path):
    """A compressed conversation history must remain reversible by ContextRef."""
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(min_chars=10, preview_max_chars=180),
    )
    set_current_context_engine(engine)

    shell_original = "\n".join(
        [f"shell filler line {idx}" for idx in range(40)]
        + ["HISTORY_NEEDLE_A secret=roundtrip-alpha"]
        + [f"shell tail line {idx}" for idx in range(40, 80)]
    )
    search_original = "\n".join(
        [f"src/example_{idx}.py:{idx}: ordinary match" for idx in range(40)]
        + ["src/target.py:777: HISTORY_NEEDLE_B secret=roundtrip-beta"]
        + [f"src/example_{idx}.py:{idx}: ordinary tail" for idx in range(40, 80)]
    )

    def count_tokens(messages, _model_id):
        text = "\n".join(_extract_content_text(msg.content) for msg in messages)
        return 9999 if "HISTORY_NEEDLE_A" in text and "ContextRef" not in text else 1

    monkeypatch.setattr(compression_module, "_count_tokens", count_tokens)
    try:
        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(
            [
                _msg(MessageRole.SYSTEM, "system"),
                _msg(MessageRole.USER, "task"),
                _msg(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}"),
                _msg(MessageRole.TOOL_RESPONSE, shell_original),
                _msg(MessageRole.TOOL_CALL, "{'name': 'grep_search', 'arguments': 'HISTORY'}"),
                _msg(MessageRole.TOOL_RESPONSE, search_original),
            ]
        )

        compressed = manager.get_compressed_messages("dummy")
        visible = "\n".join(_extract_content_text(msg.content) for msg in compressed)
        refs = [part for part in visible.split() if part.startswith("ctx_")]

        assert len(refs) == 2
        assert "Truncated" not in visible
        assert "HISTORY_NEEDLE_A" in engine.retrieve(refs[0], query="HISTORY_NEEDLE_A")
        assert "roundtrip-alpha" in engine.retrieve(refs[0], offset=40, limit=2)
        assert "HISTORY_NEEDLE_B" in engine.retrieve(refs[1], query="HISTORY_NEEDLE_B")

        compressed_again, saved = compression_module._apply_context_engine_compression(
            [InternalChatMessage.from_chat_message(msg) for msg in compressed]
        )
        assert saved == 0
        assert [_extract_content_text(msg.message.content) for msg in compressed_again] == [
            _extract_content_text(msg.content) for msg in compressed
        ]
    finally:
        clear_current_context_engine(engine)


def test_context_engine_safety_skip_tools_not_compressed(tmp_path):
    engine = ContextEngine(
        tmp_path / "context_store",
        config=ContextEngineConfig(
            min_chars=10,
            preview_max_chars=120,
            safety=ContextSafetyConfig(skip_tools=("write_file",)),
        ),
    )

    result = engine.compress_tool_result(
        "source code that should remain visible\n" * 20,
        tool_name="write_file",
        source="safety",
    )

    assert result is None
    assert engine.store.refs() == []
