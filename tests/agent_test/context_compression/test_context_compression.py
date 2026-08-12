import json
import logging
import pathlib

from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole

import src.lib.smolagents.memory.context_compression as compression_module
from src.lib.smolagents.memory.context_compression import (
    FILE_DEDUP_PLACEHOLDER,
    OBSERVATION_MASKING_PLACEHOLDER,
    ConversationHistoryManager,
    InternalChatMessage,
    SummarizeResponse,
    _apply_observation_masking,
    _apply_tool_dedup,
    _apply_tool_output_truncation,
    _extract_content_text,
    _extract_tool_invocations,
    _iter_visible_non_system_groups,
    _iter_visible_tool_response_pairs,
    _serialize_messages_for_summary,
    _split_summary_head_and_recent_tail,
    summarize_conversation,
    to_api_messages,
    to_internal_messages,
    truncate_conversation,
)

MOCK_DIR = pathlib.Path(__file__).parent


def create_mock_message(role, content_text):
    msg = ChatMessage(
        role=role,
        content=[{"type": "text", "text": content_text}],
    )
    return InternalChatMessage(message=msg)


def create_python_interpreter_call(code_text):
    return create_mock_message(
        MessageRole.TOOL_CALL,
        "{'name': 'python_interpreter', 'arguments': " + repr(code_text) + "}",
    )


def create_history_messages():
    return [
        ChatMessage(role=MessageRole.SYSTEM, content="system"),
        ChatMessage(role=MessageRole.USER, content="user request"),
        ChatMessage(role=MessageRole.ASSISTANT, content="assistant reply"),
    ]


def test_extract_tool_invocations_from_native_dict_tool_calls():
    msg = ChatMessage(
        role=MessageRole.TOOL_CALL,
        content="",
    )
    msg.tool_calls = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": {
                    "file_path": "/tmp/native.py",
                    "offset": 10,
                    "limit": 20,
                },
            },
        }
    ]

    invocations = _extract_tool_invocations(msg)

    assert [invocation.name for invocation in invocations] == ["read_file"]
    assert '"file_path": "/tmp/native.py"' in invocations[0].arguments
    assert invocations[0].dedup_key == invocations[0].arguments


def test_extract_tool_invocations_from_codeact_python_interpreter():
    code = """
content = read_file(file_path="/tmp/codeact.py", offset=1, limit=40)
result = shell_tool(commands=["printf hello"])
print(content, result)
""".strip()
    msg = ChatMessage(
        role=MessageRole.TOOL_CALL,
        content="{'name': 'python_interpreter', 'arguments': " + repr(code) + "}",
    )

    invocations = _extract_tool_invocations(msg)

    assert [invocation.name for invocation in invocations] == ["read_file", "shell_tool"]
    assert invocations[0].dedup_key == invocations[0].arguments
    assert invocations[1].dedup_key is None


def test_extract_tool_invocations_from_direct_python_ast():
    msg = ChatMessage(
        role=MessageRole.TOOL_CALL,
        content='read_file("/tmp/direct.py", offset=5, limit=10)\nprint("ignored")',
    )

    invocations = _extract_tool_invocations(msg)

    assert [invocation.name for invocation in invocations] == ["read_file"]
    assert "/tmp/direct.py" in invocations[0].arguments


def test_tool_deduplication_basic(tmp_path):
    """测试 _apply_tool_dedup 基础功能：
    真实在这个目录下创建一个临时 txt 文件，写入内容，并模拟多次读取它。
    """
    # 1. 创建真实临时文件并写入内容
    test_file = tmp_path / "real_test.txt"
    test_file.write_text("This is actual file content read from disk. " * 50)
    content_v1 = test_file.read_text()

    # 2. 模拟第一次工具调用与响应
    msg1 = create_mock_message(MessageRole.TOOL_CALL, f"read_file('{test_file}')")
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, content_v1)

    # 3. 模拟文件内容发生了更新
    test_file.write_text(content_v1 + " NEW APPENDED TEXT")
    content_v2 = test_file.read_text()

    # 4. 模拟第二次针对同一文件的调用
    msg3 = create_mock_message(MessageRole.TOOL_CALL, f"read_file('{test_file}')")
    msg4 = create_mock_message(MessageRole.TOOL_RESPONSE, content_v2)

    messages = [msg1, msg2, msg3, msg4]

    new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

    assert len(new_messages) == 4
    # 第一次较旧的响应应当被完全去重消除，替换为占位符
    assert new_messages[1].message.content[0]["text"] == FILE_DEDUP_PLACEHOLDER
    # 最后一次最新的响应应当完整保留
    assert new_messages[3].message.content[0]["text"] == content_v2
    assert saved_ratio > 0


def test_tool_deduplication_keyword_file_path(tmp_path):
    test_file = tmp_path / "keyword_args.txt"
    test_file.write_text("first version " * 40)
    content_v1 = test_file.read_text()

    msg1 = create_mock_message(
        MessageRole.TOOL_CALL,
        f"read_file(file_path='{test_file}', strip_whitespace=False)",
    )
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, content_v1)

    test_file.write_text(content_v1 + " updated")
    content_v2 = test_file.read_text()

    msg3 = create_mock_message(
        MessageRole.TOOL_CALL,
        f"read_file(file_path='{test_file}', strip_whitespace=False)",
    )
    msg4 = create_mock_message(MessageRole.TOOL_RESPONSE, content_v2)

    new_messages, saved_ratio = _apply_tool_dedup([msg1, msg2, msg3, msg4], "dummy_model", logger=None)

    assert new_messages[1].message.content[0]["text"] == FILE_DEDUP_PLACEHOLDER
    assert new_messages[3].message.content[0]["text"] == content_v2
    assert saved_ratio > 0


def test_tool_deduplication_python_interpreter_read_file_lines_same_range():
    code = """
content = read_file(
    file_path="/tmp/demo.py",
    start_line=10,
    end_line=20,
    include_line_numbers=True,
)
print(content)
""".strip()
    messages = [
        create_python_interpreter_call(code),
        create_mock_message(MessageRole.TOOL_RESPONSE, "A" * 500),
        create_python_interpreter_call(code),
        create_mock_message(MessageRole.TOOL_RESPONSE, "B" * 550),
    ]

    new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

    assert new_messages[1].message.content[0]["text"] == FILE_DEDUP_PLACEHOLDER
    assert new_messages[3].message.content[0]["text"] == "B" * 550
    assert saved_ratio > 0


def test_tool_deduplication_python_interpreter_read_file_lines_different_range_not_deduped():
    code_a = """
content = read_file(
    file_path="/tmp/demo.py",
    start_line=10,
    end_line=20,
    include_line_numbers=True,
)
print(content)
""".strip()
    code_b = """
content = read_file(
    file_path="/tmp/demo.py",
    start_line=21,
    end_line=40,
    include_line_numbers=True,
)
print(content)
""".strip()
    messages = [
        create_python_interpreter_call(code_a),
        create_mock_message(MessageRole.TOOL_RESPONSE, "range-a"),
        create_python_interpreter_call(code_b),
        create_mock_message(MessageRole.TOOL_RESPONSE, "range-b"),
    ]

    new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

    assert new_messages[1].message.content[0]["text"] == "range-a"
    assert new_messages[3].message.content[0]["text"] == "range-b"
    assert saved_ratio == 0


def test_tool_deduplication_python_interpreter_get_file_outline_same_shape():
    code = """
outline = get_file_outline(
    file_path="/tmp/demo.py",
    detail_level="full",
    include_line_numbers=False,
)
print(outline)
""".strip()
    messages = [
        create_python_interpreter_call(code),
        create_mock_message(MessageRole.TOOL_RESPONSE, "outline-1" * 120),
        create_python_interpreter_call(code),
        create_mock_message(MessageRole.TOOL_RESPONSE, "outline-2" * 120),
    ]

    new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

    assert new_messages[1].message.content[0]["text"] == FILE_DEDUP_PLACEHOLDER
    assert new_messages[3].message.content[0]["text"] == "outline-2" * 120
    assert saved_ratio > 0


def test_tool_deduplication_preserves_latest_identical_response():
    code = "content = read_file('/tmp/same-output.txt')\nprint(content)"
    messages = [
        create_python_interpreter_call(code),
        create_mock_message(MessageRole.TOOL_RESPONSE, "unchanged file content"),
        create_python_interpreter_call(code),
        create_mock_message(MessageRole.TOOL_RESPONSE, "unchanged file content"),
    ]

    new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

    assert new_messages[1].message.content[0]["text"] == FILE_DEDUP_PLACEHOLDER
    assert new_messages[3].message.content[0]["text"] == "unchanged file content"
    assert saved_ratio == 0


def test_tool_deduplication_case_insensitive():
    # 测试路径匹配忽略大小写
    msg1 = create_mock_message(MessageRole.TOOL_CALL, "READ_FILE('/tmp/test.txt')")
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, "content")

    messages = [msg1, msg2]
    new_messages, _ = _apply_tool_dedup(messages, "dummy_model", logger=None)
    # 没有重复读取，不应该改变
    assert len(new_messages) == 2
    assert new_messages[1].message.content[0]["text"] == "content"


def test_overlap_tool_truncation_with_dedup():
    # 测试工具 A 在 DEDUP 中，但没有在 MAX_RETAIN_CHARS 限制，不应被 default 截断 (3000)
    long_content = "A" * 4000

    # 我们用 read_file_content 测试（在 TOOL_MAX_RETAIN_CHARS 为 None，在 DEDUP_PATTERNS 中存在）
    msg1 = create_mock_message(MessageRole.TOOL_CALL, "{'name': 'python_interpreter', 'arguments': 'read_file(\\'/tmp/big.txt\\')'}")
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    messages = [msg1, msg2]
    new_messages, saved_chars = _apply_tool_output_truncation(messages, logger=None)

    # 应该跳过截断，保留完整原文
    assert saved_chars == 0
    assert len(new_messages[1].message.content[0]["text"]) == 4000


def test_python_interpreter_file_reads_are_exempt_from_layer2_truncation():
    long_content = "A" * 4000
    msg1 = create_python_interpreter_call(
        """
content = read_file(
    file_path="/tmp/big.txt",
    start_line=1,
    end_line=300,
    include_line_numbers=True,
)
print(content)
""".strip()
    )
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    new_messages, saved_chars = _apply_tool_output_truncation([msg1, msg2], logger=None)

    assert saved_chars == 0
    assert new_messages[1].message.content[0]["text"] == long_content


def test_python_interpreter_outline_reads_are_exempt_from_layer2_truncation():
    long_content = "B" * 4500
    msg1 = create_python_interpreter_call(
        """
outline = get_file_outline(
    file_path="/tmp/huge.c",
    detail_level="full",
    include_line_numbers=True,
)
print(outline)
""".strip()
    )
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    new_messages, saved_chars = _apply_tool_output_truncation([msg1, msg2], logger=None)

    assert saved_chars == 0
    assert new_messages[1].message.content[0]["text"] == long_content


def test_python_interpreter_shell_tool_still_truncates():
    long_content = "C" * 3200
    msg1 = create_python_interpreter_call(
        'result = shell_tool(commands=["grep -n foo /tmp/demo.txt"])'
    )
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    new_messages, saved_chars = _apply_tool_output_truncation([msg1, msg2], logger=None)

    assert saved_chars == 1200
    assert "shell_tool output" in new_messages[1].message.content[0]["text"]


def test_tool_output_truncation_uses_message_position_not_object_equality():
    long_content = "Z" * 4000
    read_call = create_python_interpreter_call("content = read_file('/tmp/large.txt')\nprint(content)")
    shell_call = create_python_interpreter_call("result = shell_tool(commands=['cat /tmp/large.txt'])\nprint(result)")
    messages = [
        read_call,
        create_mock_message(MessageRole.TOOL_RESPONSE, long_content),
        shell_call,
        create_mock_message(MessageRole.TOOL_RESPONSE, long_content),
    ]

    new_messages, saved_chars = _apply_tool_output_truncation(messages, logger=None)

    assert saved_chars == 2000
    assert new_messages[1].message.content[0]["text"] == long_content
    assert "shell_tool output" in new_messages[3].message.content[0]["text"]


def test_python_interpreter_ripgrep_still_truncates():
    long_content = "D" * 3400
    msg1 = create_python_interpreter_call(
        'result = ripgrep_search_directory(directory="/tmp", rg_args=["-n", "foo"])'
    )
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    new_messages, saved_chars = _apply_tool_output_truncation([msg1, msg2], logger=None)

    assert saved_chars == 400


def test_smart_summary_disabled_skips_layer3_and_falls_back_to_truncation(monkeypatch):
    calls = []

    monkeypatch.setattr(compression_module, "_count_tokens", lambda _messages, _model_id: 9999)

    def fake_dedup(messages, model_id, logger=None):
        calls.append("layer1")
        return messages, 0.0

    def fake_truncation(messages, logger=None):
        calls.append("layer2")
        return messages, 0

    def fail_if_summarized(*args, **kwargs):
        raise AssertionError("summarize_conversation should not be called when smart_summary is false")

    def fake_masking(messages, frac_to_mask=0.3, logger=None):
        calls.append("layer3_masking")
        return messages, 0

    def fake_truncate_until_fits(self, model_id, frac_to_remove):
        calls.append(("truncate", model_id, frac_to_remove))

    monkeypatch.setattr(compression_module, "_apply_tool_dedup", fake_dedup)
    monkeypatch.setattr(compression_module, "_apply_tool_output_truncation", fake_truncation)
    monkeypatch.setattr(compression_module, "_apply_observation_masking", fake_masking)
    monkeypatch.setattr(compression_module, "summarize_conversation", fail_if_summarized)
    monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", fake_truncate_until_fits)

    manager = ConversationHistoryManager(max_tokens=10, smart_summary=False)
    manager.sync_from_messages(create_history_messages())

    manager.get_compressed_messages(model_id="dummy-model")

    assert calls[:3] == ["layer1", "layer2", "layer3_masking"]
    assert calls[3] == (
        "truncate",
        "dummy-model",
        compression_module.TRUNCATION_FRAC_TO_REMOVE,
    )


def test_smart_summary_enabled_uses_layer4(monkeypatch):
    """When smart_summary=True, the pipeline should call layer3_masking then layer4_summary."""
    calls = []

    monkeypatch.setattr(compression_module, "_count_tokens", lambda _messages, _model_id: 9999)
    monkeypatch.setattr(compression_module, "_apply_tool_dedup", lambda messages, model_id, logger=None: (messages, 0.0))
    monkeypatch.setattr(compression_module, "_apply_tool_output_truncation", lambda messages, logger=None: (messages, 0))
    monkeypatch.setattr(compression_module, "_apply_observation_masking", lambda messages, frac_to_mask=0.3, logger=None: (calls.append("layer3_masking"), (messages, 0))[1])

    def fake_summarize(
        messages,
        model_id,
        custom_condense_prompt=None,
        cached_command_blocks=None,
        cached_skill_load=None,
        preserve_recent_tokens=None,
    ):
        calls.append(("layer4_summary", model_id))
        return SummarizeResponse(messages=messages, summary="", error="summary failed")

    def fake_truncate_until_fits(self, model_id, frac_to_remove):
        calls.append(("truncate", model_id, frac_to_remove))

    monkeypatch.setattr(compression_module, "summarize_conversation", fake_summarize)
    monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", fake_truncate_until_fits)

    manager = ConversationHistoryManager(max_tokens=10, smart_summary=True)
    manager.sync_from_messages(create_history_messages())

    manager.get_compressed_messages(model_id="dummy-model")

    assert "layer3_masking" in calls
    assert calls[1][0] == "layer4_summary"
    assert calls[2] == (
        "truncate",
        "dummy-model",
        compression_module.TRUNCATION_FRAC_TO_REMOVE,
    )


def test_malformed_payload_falls_back_to_default_truncation():
    long_content = "E" * 3500
    msg1 = create_mock_message(
        MessageRole.TOOL_CALL,
        "{'name': 'python_interpreter', 'arguments': 'read_file(/tmp/bad.txt'}",
    )
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    new_messages, saved_chars = _apply_tool_output_truncation([msg1, msg2], logger=None)

    assert saved_chars == 500
    assert "default output" in new_messages[1].message.content[0]["text"]


def test_basic_truncation():
    # 测试普通工具超过 default 3000 被截断
    long_content = "A" * 3500

    msg1 = create_mock_message(MessageRole.TOOL_CALL, "{'name': 'unknown_tool', 'arguments': ''}")
    msg2 = create_mock_message(MessageRole.TOOL_RESPONSE, long_content)

    messages = [msg1, msg2]
    new_messages, saved_chars = _apply_tool_output_truncation(messages, logger=None)

    # unknown_tool 应该触发 default = 3000 截断
    assert saved_chars == 500
    truncated_text = new_messages[1].message.content[0]["text"]
    assert "Truncated 500 characters" in truncated_text
    # 头部和尾部各 1500，加上提示词的长度，大约 3000 出头
    assert len(truncated_text) < 3200


def test_tool_deduplication_from_real_logs():
    """
    通过读取一个真实的类似 smolagents 输出格式的 test.log 文件内容，
    验证去重机制确实能在这种文本序列中抓取关键工具调用并替换旧的响应。
    """
    import pathlib
    log_path = pathlib.Path(__file__).parent / "test.log"
    log_content = log_path.read_text(encoding="utf-8")

    # 解析真实日志流并粗略还原为 Messages 列表
    messages = []

    lines = log_content.splitlines()
    in_code_block = False
    in_log_block = False
    code_lines = []
    log_lines = []

    for line in lines:
        if "─ Executing parsed code:" in line:
            in_code_block = True
            code_lines = []
            continue
        elif in_code_block and "─" * 10 in line:
            in_code_block = False
            if code_lines:
                parsed_code = "\n".join(code_lines).strip()
                messages.append(InternalChatMessage(message=ChatMessage(
                    role=MessageRole.TOOL_CALL,
                    content=parsed_code
                )))
            continue

        if "Execution logs:" in line:
            in_log_block = True
            log_lines = []
            continue
        elif in_log_block and "Out:" in line:
            in_log_block = False
            if log_lines:
                parsed_logs = "\n".join(log_lines).strip()
                messages.append(InternalChatMessage(message=ChatMessage(
                    role=MessageRole.TOOL_RESPONSE,
                    content=[{"type": "text", "text": parsed_logs}]
                )))
            continue

        if in_code_block:
            code_lines.append(line)
        if in_log_block:
            log_lines.append(line)

    assert len(messages) == 4

    # 执行去重逻辑
    new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

    assert len(new_messages) == 4

    # 验证旧的被去重了
    assert new_messages[1].message.content[0]["text"] == FILE_DEDUP_PLACEHOLDER

    # 验证最后的响应是最新的内容
    assert "CANIF_VERSION 2.0" in new_messages[3].message.content[0]["text"]


# =============================================================================
# Helper: load mock JSON and convert to InternalChatMessage list
# =============================================================================
def _load_mock_messages(filename: str) -> list[InternalChatMessage]:
    """Load a mock JSON file and convert to InternalChatMessage list."""
    data = json.loads((MOCK_DIR / filename).read_text(encoding="utf-8"))
    messages = []
    for item in data:
        role_str = item["role"]
        role_map = {
            "system": MessageRole.SYSTEM,
            "user": MessageRole.USER,
            "assistant": MessageRole.ASSISTANT,
            "tool-call": MessageRole.TOOL_CALL,
            "tool-response": MessageRole.TOOL_RESPONSE,
        }
        role = role_map[role_str]
        content = item["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        msg = ChatMessage(role=role, content=content)
        messages.append(InternalChatMessage(message=msg))
    return messages


# =============================================================================
# Layer 1 extra tests
# =============================================================================
class TestLayer1DedupExtra:
    def test_dedup_from_mock_repeated_reads(self):
        """4 reads of the same file from mock JSON → only latest kept."""
        messages = _load_mock_messages("mock_repeated_file_reads.json")
        # There are 4 tool_response messages (indices 3, 5, 7, 9)
        tool_responses = [
            m for m in messages if m.message.role == MessageRole.TOOL_RESPONSE
        ]
        assert len(tool_responses) == 4

        new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)

        # The first 3 tool responses should be replaced by placeholder
        tr_texts = [
            _extract_content_text(m.message.content)
            for m in new_messages
            if m.message.role == MessageRole.TOOL_RESPONSE
        ]
        placeholder_count = sum(1 for t in tr_texts if t == FILE_DEDUP_PLACEHOLDER)
        assert placeholder_count == 3  # 3 old reads deduped
        assert "v1.2" in tr_texts[-1]  # latest read preserved
        assert saved_ratio > 0

    def test_dedup_no_tool_response_messages(self):
        """All user/assistant messages → saved_ratio == 0, no changes."""
        messages = [
            create_mock_message(MessageRole.USER, "Hello"),
            create_mock_message(MessageRole.ASSISTANT, "Hi there"),
            create_mock_message(MessageRole.USER, "How are you?"),
            create_mock_message(MessageRole.ASSISTANT, "I'm fine."),
        ]
        new_messages, saved_ratio = _apply_tool_dedup(messages, "dummy_model", logger=None)
        assert saved_ratio == 0
        assert len(new_messages) == 4


# =============================================================================
# Layer 2 extra tests
# =============================================================================
class TestLayer2TruncationExtra:
    def test_multiple_tools_mixed_truncation(self):
        """shell_tool(2000), ripgrep(3000), unknown(3000 default) all over limit."""
        messages = []
        # shell_tool call + response (4000 chars, limit=2000)
        messages.append(create_mock_message(
            MessageRole.TOOL_CALL,
            "{'name': 'python_interpreter', 'arguments': 'result = shell_tool(commands=[\"ls -la /\"])\\nprint(result)'}",
        ))
        messages.append(create_mock_message(MessageRole.TOOL_RESPONSE, "S" * 4000))

        # ripgrep call + response (5000 chars, limit=3000)
        messages.append(create_mock_message(
            MessageRole.TOOL_CALL,
            "{'name': 'python_interpreter', 'arguments': 'result = ripgrep_search_directory(directory=\"/tmp\", rg_args=[\"-n\", \"foo\"])\\nprint(result)'}",
        ))
        messages.append(create_mock_message(MessageRole.TOOL_RESPONSE, "R" * 5000))

        # unknown_tool call + response (4500 chars, default limit=3000)
        messages.append(create_mock_message(
            MessageRole.TOOL_CALL,
            "{'name': 'unknown_tool', 'arguments': ''}",
        ))
        messages.append(create_mock_message(MessageRole.TOOL_RESPONSE, "U" * 4500))

        new_messages, saved_chars = _apply_tool_output_truncation(messages, logger=None)

        assert saved_chars > 0
        # shell: 4000-2000=2000 saved, ripgrep: 5000-3000=2000, unknown: 4500-3000=1500
        # total approx 5500
        assert saved_chars >= 5000

    def test_layer2_short_circuit_when_under_limit(self, monkeypatch):
        """After Layer 2 truncation, if tokens are under limit, pipeline returns early."""
        calls = []

        # _count_tokens is called:
        # 1st: initial check → over limit
        # 2nd: after Layer 2 truncation → under limit (short-circuit)
        call_count = {"n": 0}
        def smart_count_tokens(_messages, _model_id):
            call_count["n"] += 1
            # 1st call: initial check in get_compressed_messages
            if call_count["n"] == 1:
                return 9999  # over limit
            # 2nd call: after Layer 2 truncation saved chars → should be under limit
            return 50

        monkeypatch.setattr(compression_module, "_count_tokens", smart_count_tokens)
        monkeypatch.setattr(compression_module, "_apply_tool_dedup", lambda m, model_id=None, logger=None: (calls.append("layer1"), (m, 0.0))[1])

        def fake_truncation(messages, logger=None):
            calls.append("layer2")
            return messages, 100  # pretend we saved something

        monkeypatch.setattr(compression_module, "_apply_tool_output_truncation", fake_truncation)

        def fail_layer3(*args, **kwargs):
            calls.append("layer3_masking")
            raise AssertionError("Layer 3 should not be called when Layer 2 resolves it")

        monkeypatch.setattr(compression_module, "_apply_observation_masking", fail_layer3)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(create_history_messages())
        manager.get_compressed_messages(model_id="dummy-model")

        assert calls == ["layer1", "layer2"]


# =============================================================================
# Layer 3 tests (Observation Masking) — all new
# =============================================================================
class TestLayer3ObservationMasking:
    def _make_tool_pair(self, idx: int, content: str) -> list[InternalChatMessage]:
        """Create a tool_call + tool_response pair."""
        call = create_mock_message(
            MessageRole.TOOL_CALL,
            f"{{'name': 'python_interpreter', 'arguments': 'read_file_{idx}()'}}",
        )
        resp = create_mock_message(MessageRole.TOOL_RESPONSE, content)
        return [call, resp]

    def test_basic_masking(self):
        """10 tool_responses, frac=0.3 → mask oldest 3, keep newest 7."""
        messages = []
        for i in range(10):
            messages.extend(self._make_tool_pair(i, f"content_{i} " * 200))

        new_msgs, chars_saved = _apply_observation_masking(messages, frac_to_mask=0.3)

        # Count masked responses
        masked = [
            m for m in new_msgs
            if m.message.role == MessageRole.TOOL_RESPONSE
            and _extract_content_text(m.message.content) == OBSERVATION_MASKING_PLACEHOLDER
        ]
        assert len(masked) == 3
        assert chars_saved > 0

    def test_masking_preserves_tool_call(self):
        """Tool call messages should never be modified by masking."""
        messages = []
        for i in range(6):
            messages.extend(self._make_tool_pair(i, f"data_{i} " * 300))

        new_msgs, _ = _apply_observation_masking(messages, frac_to_mask=0.3)

        # All tool_call messages should be unchanged
        original_calls = [m for m in messages if m.message.role == MessageRole.TOOL_CALL]
        new_calls = [m for m in new_msgs if m.message.role == MessageRole.TOOL_CALL]
        assert len(original_calls) == len(new_calls)
        for orig, new in zip(original_calls, new_calls, strict=True):
            assert _extract_content_text(orig.message.content) == _extract_content_text(new.message.content)

    def test_masking_chars_saved_calculation(self):
        """chars_saved should equal sum of (original - placeholder) for masked msgs."""
        content_size = 500
        messages = []
        for i in range(4):
            messages.extend(self._make_tool_pair(i, "X" * content_size))

        new_msgs, chars_saved = _apply_observation_masking(messages, frac_to_mask=0.5)

        # 2 out of 4 masked
        expected_saved = 2 * (content_size - len(OBSERVATION_MASKING_PLACEHOLDER))
        assert chars_saved == expected_saved

    def test_masking_no_tool_responses(self):
        """All user/assistant → no masking, chars_saved == 0."""
        messages = [
            create_mock_message(MessageRole.USER, "Hello"),
            create_mock_message(MessageRole.ASSISTANT, "Hi"),
        ]
        new_msgs, chars_saved = _apply_observation_masking(messages)
        assert chars_saved == 0
        assert len(new_msgs) == 2

    def test_masking_single_tool_response(self):
        """Only 1 tool_response → int(1 * 0.3) = 0 → nothing masked."""
        messages = self._make_tool_pair(0, "single content " * 200)
        new_msgs, chars_saved = _apply_observation_masking(messages, frac_to_mask=0.3)
        assert chars_saved == 0

    def test_masking_skips_already_masked(self):
        """Already-masked responses should not be re-counted."""
        messages = []
        # First pair: already masked
        call = create_mock_message(MessageRole.TOOL_CALL, "{'name': 'tool', 'arguments': ''}")
        resp = create_mock_message(MessageRole.TOOL_RESPONSE, OBSERVATION_MASKING_PLACEHOLDER)
        messages.extend([call, resp])
        # Two more normal pairs
        for i in range(2):
            messages.extend(self._make_tool_pair(i, f"real_data_{i} " * 200))

        new_msgs, chars_saved = _apply_observation_masking(messages, frac_to_mask=0.5)
        # Only 2 real responses eligible, int(2*0.5)=1 should be masked
        masked = [
            m for m in new_msgs
            if m.message.role == MessageRole.TOOL_RESPONSE
            and _extract_content_text(m.message.content) == OBSERVATION_MASKING_PLACEHOLDER
        ]
        # 1 already masked + 1 newly masked = 2
        assert len(masked) == 2

    def test_masking_skips_dedup_placeholder(self):
        """Responses already deduped (FILE_DEDUP_PLACEHOLDER) should not be re-masked."""
        messages = []
        call = create_mock_message(MessageRole.TOOL_CALL, "{'name': 'tool', 'arguments': ''}")
        resp = create_mock_message(MessageRole.TOOL_RESPONSE, FILE_DEDUP_PLACEHOLDER)
        messages.extend([call, resp])
        for i in range(2):
            messages.extend(self._make_tool_pair(i, f"content_{i} " * 200))

        new_msgs, chars_saved = _apply_observation_masking(messages, frac_to_mask=0.5)
        # Only 2 eligible, mask 1
        dedup_resp = [
            m for m in new_msgs
            if m.message.role == MessageRole.TOOL_RESPONSE
            and _extract_content_text(m.message.content) == FILE_DEDUP_PLACEHOLDER
        ]
        assert len(dedup_resp) == 1  # dedup placeholder untouched

    def test_masking_preserves_skill_tool_response(self):
        messages = [
            create_mock_message(
                MessageRole.TOOL_CALL,
                "{'name': 'skill', 'arguments': {'name': 'agent-recall-with-files'}}",
            ),
            create_mock_message(MessageRole.TOOL_RESPONSE, "skill instructions " * 200),
        ]
        for idx in range(4):
            messages.extend(self._make_tool_pair(idx, f"content_{idx} " * 200))

        new_msgs, _ = _apply_observation_masking(messages, frac_to_mask=0.5)

        skill_response = new_msgs[1]
        assert _extract_content_text(skill_response.message.content) == "skill instructions " * 200

        masked = [
            m for m in new_msgs
            if m.message.role == MessageRole.TOOL_RESPONSE
            and _extract_content_text(m.message.content) == OBSERVATION_MASKING_PLACEHOLDER
        ]
        assert masked


# =============================================================================
# Layer 4 tests (LLM summary — mocked)
# =============================================================================
class TestLayer4Summary:
    def test_summary_success_then_under_limit(self, monkeypatch):
        """After successful summarization, if under limit, no truncation needed."""
        calls = []
        call_count = {"n": 0}

        def smart_count(_msgs, _mid):
            # _count_tokens is called:
            # 1: initial check → over limit
            # 2: after summary success, check if under limit → under limit
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 9999  # initial: over limit
            return 50  # after summary: under limit

        monkeypatch.setattr(compression_module, "_count_tokens", smart_count)
        monkeypatch.setattr(compression_module, "_apply_tool_dedup", lambda m, model_id=None, logger=None: (m, 0.0))
        monkeypatch.setattr(compression_module, "_apply_tool_output_truncation", lambda m, logger=None: (m, 0))
        monkeypatch.setattr(compression_module, "_apply_observation_masking", lambda m, frac_to_mask=0.3, logger=None: (m, 0))

        def fake_summarize(
            messages,
            model_id,
            custom_condense_prompt=None,
            cached_command_blocks=None,
            cached_skill_load=None,
            preserve_recent_tokens=None,
        ):
            calls.append("layer4_summary")
            return SummarizeResponse(messages=messages, summary="summary", error=None)

        def fail_truncate(self, model_id, frac_to_remove):
            raise AssertionError("truncate_until_fits should not be called")

        monkeypatch.setattr(compression_module, "summarize_conversation", fake_summarize)
        monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", fail_truncate)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=True)
        manager.sync_from_messages(create_history_messages())
        manager.get_compressed_messages(model_id="dummy-model")

        assert "layer4_summary" in calls

    def test_summary_success_but_still_over_limit(self, monkeypatch):
        """After summary, still over limit → truncation is called."""
        calls = []
        monkeypatch.setattr(compression_module, "_count_tokens", lambda _m, _mid: 9999)
        monkeypatch.setattr(compression_module, "_apply_tool_dedup", lambda m, model_id=None, logger=None: (m, 0.0))
        monkeypatch.setattr(compression_module, "_apply_tool_output_truncation", lambda m, logger=None: (m, 0))
        monkeypatch.setattr(compression_module, "_apply_observation_masking", lambda m, frac_to_mask=0.3, logger=None: (m, 0))

        def fake_summarize(
            messages,
            model_id,
            custom_condense_prompt=None,
            cached_command_blocks=None,
            cached_skill_load=None,
            preserve_recent_tokens=None,
        ):
            calls.append("layer4_summary")
            return SummarizeResponse(messages=messages, summary="summary", error=None)

        def fake_truncate(self, model_id, frac_to_remove):
            calls.append("truncate")

        monkeypatch.setattr(compression_module, "summarize_conversation", fake_summarize)
        monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", fake_truncate)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=True)
        manager.sync_from_messages(create_history_messages())
        manager.get_compressed_messages(model_id="dummy-model")

        assert "layer4_summary" in calls
        assert "truncate" in calls


# =============================================================================
# Fallback: truncate_conversation direct tests
# =============================================================================
class TestTruncateConversation:
    def _make_visible_messages(self, count: int) -> list[InternalChatMessage]:
        """Create system + N visible non-system messages."""
        msgs = [create_mock_message(MessageRole.SYSTEM, "system prompt")]
        for i in range(count):
            role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
            msgs.append(create_mock_message(role, f"message_{i} " * 100))
        return msgs

    def test_basic_truncation_removes_messages(self):
        """6 visible messages, frac=0.3 → removes the oldest single-message group."""
        messages = self._make_visible_messages(6)
        result = truncate_conversation(messages, frac_to_remove=0.3)
        assert result.messages_removed == 1

    def test_group_truncation_does_not_even_align_plain_messages(self):
        """Plain messages are single groups; only tool-call/tool-response pairs are atomic."""
        messages = self._make_visible_messages(5)
        result = truncate_conversation(messages, frac_to_remove=0.3)
        assert result.messages_removed == 1

    def test_very_few_messages_still_works(self):
        """2 visible messages still make progress by removing the oldest group."""
        messages = self._make_visible_messages(2)
        result = truncate_conversation(messages, frac_to_remove=0.3)
        assert result.messages_removed == 1

    def test_inserts_truncation_marker(self):
        """After truncation, a marker message with is_truncation_marker=True exists."""
        messages = self._make_visible_messages(8)
        result = truncate_conversation(messages, frac_to_remove=0.3)
        assert result.messages_removed > 0
        markers = [m for m in result.messages if m.is_truncation_marker]
        assert len(markers) == 1
        marker_text = _extract_content_text(markers[0].message.content)
        assert "Sliding window truncation" in marker_text

    def test_preserves_system_messages(self):
        """System messages should never be truncated."""
        messages = self._make_visible_messages(10)
        result = truncate_conversation(messages, frac_to_remove=0.5)
        system_msgs = [
            m for m in result.messages
            if m.message.role == MessageRole.SYSTEM and m.is_visible()
        ]
        assert len(system_msgs) == 1

    def test_preserves_visible_summary_messages(self):
        """Summary messages are the recovery point after smart compact."""
        messages = [
            create_mock_message(MessageRole.SYSTEM, "system prompt"),
            create_mock_message(MessageRole.USER, "old user request " * 100),
            InternalChatMessage(
                message=ChatMessage(
                    role=MessageRole.USER,
                    content=[{"type": "text", "text": "## Conversation Summary\nimportant state"}],
                ),
                is_summary=True,
            ),
            create_mock_message(MessageRole.ASSISTANT, "newer assistant state " * 100),
        ]

        result = truncate_conversation(messages, frac_to_remove=1.0)
        summary_messages = [message for message in result.messages if message.is_summary]

        assert len(summary_messages) == 1
        assert summary_messages[0].is_visible()
        assert summary_messages[0].truncation_parent is None
        assert "## Conversation Summary" in _extract_content_text(summary_messages[0].message.content)

    def test_large_frac_removes_all(self):
        """frac=1.0 → removes all visible (even-aligned)."""
        messages = self._make_visible_messages(10)
        result = truncate_conversation(messages, frac_to_remove=1.0)
        assert result.messages_removed == 10  # 10 is already even

    def test_truncation_marker_keeps_command_reminder_only(self):
        messages = self._make_visible_messages(8)
        result = truncate_conversation(
            messages,
            frac_to_remove=0.3,
            cached_command_blocks="<command>demo</command>",
        )
        markers = [m for m in result.messages if m.is_truncation_marker]
        assert len(markers) == 1
        marker_text = _extract_content_text(markers[0].message.content)
        assert "Active Workflows" in marker_text
        assert "<command>demo</command>" in marker_text
        assert "Active Skill Rules" not in marker_text

    def test_truncation_marker_can_include_recent_skill_load(self):
        messages = self._make_visible_messages(8)
        result = truncate_conversation(
            messages,
            frac_to_remove=0.3,
            cached_skill_load="<skill_name>agent-recall-with-files</skill_name>",
        )
        markers = [m for m in result.messages if m.is_truncation_marker]
        assert len(markers) == 1
        marker_text = _extract_content_text(markers[0].message.content)
        assert "Recent Skill Load" in marker_text
        assert "<skill_name>agent-recall-with-files</skill_name>" in marker_text

    def test_truncation_preserves_tool_pair_when_only_plain_group_removed(self):
        messages = [
            create_mock_message(MessageRole.SYSTEM, "system prompt"),
            create_mock_message(MessageRole.USER, "old user request"),
            create_mock_message(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}"),
            create_mock_message(MessageRole.TOOL_RESPONSE, "old shell output"),
            create_mock_message(MessageRole.ASSISTANT, "newer assistant state"),
        ]

        result = truncate_conversation(messages, frac_to_remove=0.3)
        visible = to_api_messages(result.messages)
        visible_roles = [msg.role for msg in visible]

        assert result.messages_removed == 1
        assert visible_roles == [
            MessageRole.SYSTEM,
            MessageRole.USER,
            MessageRole.TOOL_CALL,
            MessageRole.TOOL_RESPONSE,
            MessageRole.ASSISTANT,
        ]
        tool_response_idx = visible_roles.index(MessageRole.TOOL_RESPONSE)
        assert visible_roles[tool_response_idx - 1] == MessageRole.TOOL_CALL


# =============================================================================
# Fallback: truncate_until_fits direct tests
# =============================================================================
class TestTruncateUntilFits:
    def test_converges_to_under_limit(self, monkeypatch):
        """With enough messages, truncation loop should converge."""
        # Build a manager with many messages
        messages = [ChatMessage(role=MessageRole.SYSTEM, content="sys")]
        for i in range(20):
            role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
            messages.append(ChatMessage(role=role, content=f"msg_{i} " * 50))

        # Token counter that decreases with fewer messages
        def count_by_length(msgs, _mid):
            return sum(len(_extract_content_text(m.content)) for m in msgs)

        monkeypatch.setattr(compression_module, "_count_tokens", count_by_length)

        total = count_by_length(messages, "dummy")
        # Set max_tokens to ~40% of total so truncation must work
        max_tokens = int(total * 0.4)

        manager = ConversationHistoryManager(max_tokens=max_tokens, smart_summary=False)
        manager.sync_from_messages(messages)
        manager.truncate_until_fits(model_id="dummy", frac_to_remove=0.3)

        final_tokens = count_by_length(to_api_messages(manager._internal_message_history), "dummy")
        assert final_tokens <= max_tokens

    def test_stops_when_no_more_removable(self, monkeypatch):
        """With very few messages, loop exits without infinite looping."""
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="A" * 500),
        ]
        # Always over limit (only 1 visible non-system message, can't pair-remove)
        monkeypatch.setattr(compression_module, "_count_tokens", lambda _m, _mid: 9999)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(messages)

        # Should NOT hang — the content-level fallback has nothing to mask (user msg, not tool_response)
        # so it breaks out with a warning
        manager.truncate_until_fits(model_id="dummy", frac_to_remove=0.3)
        # If we get here, it didn't hang — success

    def test_content_level_fallback_masks_tool_response(self, monkeypatch):
        """When message-level truncation returns 0 but there are tool_responses,
        content-level fallback should mask the oldest one."""
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.TOOL_CALL, content="call"),
            ChatMessage(role=MessageRole.TOOL_RESPONSE, content=[{"type": "text", "text": "X" * 5000}]),
        ]
        # Always over limit
        call_count = {"n": 0}
        def decreasing_count(_m, _mid):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return 9999
            return 50  # under limit after masking

        monkeypatch.setattr(compression_module, "_count_tokens", decreasing_count)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(messages)
        manager.truncate_until_fits(model_id="dummy", frac_to_remove=0.3)

        # The tool_response should now be masked
        for msg in manager._internal_message_history:
            if msg.message.role == MessageRole.TOOL_RESPONSE and msg.is_visible():
                text = _extract_content_text(msg.message.content)
                assert text == OBSERVATION_MASKING_PLACEHOLDER

    def test_content_level_fallback_preserves_skill_tool_response(self, monkeypatch):
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(
                role=MessageRole.TOOL_CALL,
                content="{'name': 'skill', 'arguments': {'name': 'agent-recall-with-files'}}",
            ),
            ChatMessage(role=MessageRole.TOOL_RESPONSE, content=[{"type": "text", "text": "skill instructions " * 500}]),
        ]
        monkeypatch.setattr(compression_module, "_count_tokens", lambda _m, _mid: 9999)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(messages)
        manager.truncate_until_fits(model_id="dummy", frac_to_remove=0.3)

        visible_tool_responses = [
            _extract_content_text(msg.message.content)
            for msg in manager._internal_message_history
            if msg.message.role == MessageRole.TOOL_RESPONSE and msg.is_visible()
        ]
        assert visible_tool_responses == ["skill instructions " * 500]

    def test_all_exhausted_logs_warning(self, monkeypatch, caplog):
        """When all strategies are exhausted, a WARNING should be logged.

        Uses caplog (stdlib) to capture the warning.  LoggerAdapter._dispatch
        mirrors all log output to the stdlib logging hierarchy via
        _stdlib_emit, so caplog always sees the records regardless of whether
        a global EnhancedAgentLogger backend is active.
        """
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="short"),
        ]
        monkeypatch.setattr(compression_module, "_count_tokens", lambda _m, _mid: 9999)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
        manager.sync_from_messages(messages)

        with caplog.at_level(logging.WARNING):
            manager.truncate_until_fits(model_id="dummy", frac_to_remove=0.3)

        assert any(
            "All compression strategies exhausted" in record.message
            for record in caplog.records
        ), f"Expected 'All compression strategies exhausted' warning, got: {[r.message for r in caplog.records]}"


# =============================================================================
# Integration tests
# =============================================================================
class TestIntegration:
    def test_pipeline_layer_order(self, monkeypatch):
        """Full pipeline calls layers in order: L1 → L2 → L3 → L4 → Fallback."""
        calls = []

        monkeypatch.setattr(compression_module, "_count_tokens", lambda _m, _mid: 9999)

        def track(name):
            def fn(*args, **kwargs):
                calls.append(name)
                if name == "layer1":
                    return args[0], 0.0
                elif name in ("layer2", "layer3_masking"):
                    return args[0], 0
                elif name == "layer4_summary":
                    return SummarizeResponse(messages=args[0], summary="", error="fail")
                elif name == "fallback":
                    pass
            return fn

        monkeypatch.setattr(compression_module, "_apply_tool_dedup",
                            lambda m, model_id=None, logger=None: (calls.append("layer1"), (m, 0.0))[1])
        monkeypatch.setattr(compression_module, "_apply_tool_output_truncation",
                            lambda m, logger=None: (calls.append("layer2"), (m, 0))[1])
        monkeypatch.setattr(compression_module, "_apply_observation_masking",
                            lambda m, frac_to_mask=0.3, logger=None: (calls.append("layer3_masking"), (m, 0))[1])

        def fake_summarize(
            messages,
            model_id,
            custom_condense_prompt=None,
            cached_command_blocks=None,
            cached_skill_load=None,
            preserve_recent_tokens=None,
        ):
            calls.append("layer4_summary")
            return SummarizeResponse(messages=messages, summary="", error="fail")

        def fake_truncate(self, model_id, frac_to_remove):
            calls.append("fallback")

        monkeypatch.setattr(compression_module, "summarize_conversation", fake_summarize)
        monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", fake_truncate)

        manager = ConversationHistoryManager(max_tokens=100, smart_summary=True)
        manager.sync_from_messages(create_history_messages())
        manager.get_compressed_messages(model_id="dummy-model")

        assert calls == ["layer1", "layer2", "layer3_masking", "layer4_summary", "fallback"]

    def test_few_messages_large_content_resolves(self, monkeypatch):
        """The core failure scenario: few messages with large content.
        With the new Layer 3 observation masking, this should be resolvable."""
        messages = _load_mock_messages("mock_few_messages_large_content.json")

        # Use a simple char-based token counter for deterministic testing
        def count_chars(msgs, _mid):
            return sum(len(_extract_content_text(m.content)) for m in msgs)

        monkeypatch.setattr(compression_module, "_count_tokens", count_chars)

        total_chars = count_chars(to_api_messages(messages), "dummy")
        # Set max_tokens to ~60% of total to force compression
        max_tokens = int(total_chars * 0.6)

        manager = ConversationHistoryManager(max_tokens=max_tokens, smart_summary=False)
        chat_messages = [m.message for m in messages]
        manager.sync_from_messages(chat_messages)
        result = manager.get_compressed_messages(model_id="dummy-model")

        final_chars = count_chars(result, "dummy")
        assert final_chars <= max_tokens

    def test_many_messages_normal_converges(self, monkeypatch):
        """Normal multi-turn conversation compresses to within limits."""
        messages = _load_mock_messages("mock_many_messages_normal.json")

        def count_chars(msgs, _mid):
            return sum(len(_extract_content_text(m.content)) for m in msgs)

        monkeypatch.setattr(compression_module, "_count_tokens", count_chars)

        total_chars = count_chars(to_api_messages(messages), "dummy")
        max_tokens = int(total_chars * 0.5)

        manager = ConversationHistoryManager(max_tokens=max_tokens, smart_summary=False)
        chat_messages = [m.message for m in messages]
        manager.sync_from_messages(chat_messages)
        result = manager.get_compressed_messages(model_id="dummy-model")

        final_chars = count_chars(result, "dummy")
        assert final_chars <= max_tokens

def test_standard_pipeline_keeps_tool_pairs_structurally_valid(monkeypatch):
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="system"),
        ChatMessage(role=MessageRole.USER, content="Investigate the failing test."),
        ChatMessage(role=MessageRole.TOOL_CALL, content="{'name': 'shell_tool', 'arguments': ''}"),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content=[{"type": "text", "text": "X" * 5000}]),
        ChatMessage(role=MessageRole.ASSISTANT, content="The shell output shows the root cause."),
    ]

    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda msgs, _mid: sum(len(_extract_content_text(m.content)) for m in msgs),
    )

    manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
    manager.sync_from_messages(messages)
    result = manager.get_compressed_messages(model_id="dummy-model")

    for idx, msg in enumerate(result):
        if msg.role == MessageRole.TOOL_RESPONSE:
            assert idx > 0
            assert result[idx - 1].role == MessageRole.TOOL_CALL


def test_parallel_tool_results_share_one_atomic_compression_group():
    call = InternalChatMessage(
        message=ChatMessage(
            role=MessageRole.TOOL_CALL,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call-one",
                    type="function",
                    function=ChatMessageToolCallFunction(name="shell_tool", arguments={"commands": ["one"]}),
                ),
                ChatMessageToolCall(
                    id="call-two",
                    type="function",
                    function=ChatMessageToolCallFunction(name="shell_tool", arguments={"commands": ["two"]}),
                ),
            ],
        )
    )
    first = create_mock_message(MessageRole.TOOL_RESPONSE, "A" * 4000)
    second = create_mock_message(MessageRole.TOOL_RESPONSE, "B" * 4000)
    messages = [
        create_mock_message(MessageRole.USER, "run both"),
        call,
        first,
        second,
        create_mock_message(MessageRole.ASSISTANT, "continue"),
    ]

    pairs = _iter_visible_tool_response_pairs(messages)
    groups = _iter_visible_non_system_groups(messages)
    truncated, chars_saved = _apply_tool_output_truncation(messages)

    assert [pair.response_index for pair in pairs] == [2, 3]
    assert [pair.invocations[0].name for pair in pairs] == ["shell_tool", "shell_tool"]
    assert [group.indices for group in groups] == [(0,), (1, 2, 3), (4,)]
    assert chars_saved == 4000
    assert len(_extract_content_text(truncated[2].message.content)) < 4000
    assert len(_extract_content_text(truncated[3].message.content)) < 4000


def test_parallel_tool_results_are_never_orphaned_by_fallback_truncation():
    call = InternalChatMessage(
        message=ChatMessage(
            role=MessageRole.TOOL_CALL,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call-one",
                    type="function",
                    function=ChatMessageToolCallFunction(name="shell_tool", arguments={"commands": ["one"]}),
                ),
                ChatMessageToolCall(
                    id="call-two",
                    type="function",
                    function=ChatMessageToolCallFunction(name="shell_tool", arguments={"commands": ["two"]}),
                ),
            ],
        )
    )
    messages = [
        create_mock_message(MessageRole.USER, "old turn"),
        create_mock_message(MessageRole.ASSISTANT, "old answer"),
        call,
        create_mock_message(MessageRole.TOOL_RESPONSE, "first"),
        create_mock_message(MessageRole.TOOL_RESPONSE, "second"),
        create_mock_message(MessageRole.ASSISTANT, "continue"),
    ]

    result = truncate_conversation(messages, frac_to_remove=0.5)
    visible_roles = [message.role for message in to_api_messages(result.messages)]

    assert visible_roles.count(MessageRole.TOOL_CALL) in {0, 1}
    if MessageRole.TOOL_CALL not in visible_roles:
        assert MessageRole.TOOL_RESPONSE not in visible_roles
    else:
        call_index = visible_roles.index(MessageRole.TOOL_CALL)
        assert visible_roles[call_index + 1 : call_index + 3] == [
            MessageRole.TOOL_RESPONSE,
            MessageRole.TOOL_RESPONSE,
        ]


def test_summary_serialization_truncates_large_tool_results():
    messages = [
        create_mock_message(MessageRole.USER, "Please inspect the logs."),
        create_mock_message(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': 'cat /tmp/log'}"),
        create_mock_message(MessageRole.TOOL_RESPONSE, "A" * 5000),
        create_mock_message(MessageRole.ASSISTANT, "The log points at timeout handling."),
    ]

    serialized = _serialize_messages_for_summary(messages)

    assert "<conversation>" in serialized
    assert "[Tool call]:" in serialized
    assert "[Tool result]:" in serialized
    assert "characters truncated for summary" in serialized
    assert "A" * 5000 not in serialized


def test_sync_rebuilds_when_same_length_history_changes():
    manager = ConversationHistoryManager(max_tokens=1000)
    manager.sync_from_messages(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="first task"),
        ]
    )

    manager.sync_from_messages(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="replacement task"),
        ]
    )

    visible = to_api_messages(manager.get_internal_messages())
    assert [_extract_content_text(message.content) for message in visible] == [
        "system",
        "replacement task",
    ]
    assert "replacement task" in (manager._cached_command_blocks or "")
    assert "first task" not in (manager._cached_command_blocks or "")


def test_large_dedup_saving_is_remeasured_before_short_circuit(monkeypatch):
    calls = []
    messages = create_history_messages()

    monkeypatch.setattr(compression_module, "_count_tokens", lambda _messages, _model_id: 9999)
    monkeypatch.setattr(
        compression_module,
        "_apply_tool_dedup",
        lambda current, model_id, logger=None: (current, 0.5),
    )
    monkeypatch.setattr(
        compression_module,
        "_apply_context_engine_compression",
        lambda current, logger=None: (calls.append("context_engine"), (current, 0))[1],
    )
    monkeypatch.setattr(
        compression_module,
        "_apply_tool_output_truncation",
        lambda current, logger=None: (current, 0),
    )
    monkeypatch.setattr(
        compression_module,
        "_apply_observation_masking",
        lambda current, frac_to_mask=0.3, logger=None: (current, 0),
    )
    monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", lambda *args, **kwargs: None)

    manager = ConversationHistoryManager(max_tokens=10, smart_summary=False)
    manager.sync_from_messages(messages)
    manager.get_compressed_messages(model_id="dummy-model")

    assert calls == ["context_engine"]


def test_structured_tool_error_is_exempt_from_observation_masking():
    messages = []
    contents = [
        '{"ok":false,"status":"error","error":{"kind":"shell_command_error","message":"boom"}}',
        "success one " * 100,
        "success two " * 100,
        "success three " * 100,
    ]
    for index, content in enumerate(contents):
        messages.extend(
            [
                create_mock_message(MessageRole.TOOL_CALL, f"{{'name': 'shell_tool', 'arguments': '{index}'}}"),
                create_mock_message(MessageRole.TOOL_RESPONSE, content),
            ]
        )

    compressed, _ = _apply_observation_masking(messages, frac_to_mask=0.75)

    assert _extract_content_text(compressed[1].message.content) == contents[0]


def test_structured_tool_error_remains_valid_json_after_hard_truncation():
    error_content = json.dumps(
        {
            "ok": False,
            "status": "error",
            "error": {
                "kind": "shell_command_error",
                "message": "failure details " * 500,
                "retryable": True,
                "stage": "tool_execution",
            },
        }
    )
    messages = [
        create_mock_message(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}"),
        create_mock_message(MessageRole.TOOL_RESPONSE, error_content),
    ]

    compressed, chars_saved = _apply_tool_output_truncation(messages)
    preserved = _extract_content_text(compressed[1].message.content)

    assert chars_saved == 0
    assert json.loads(preserved)["error"]["message"].startswith("failure details")


def test_old_structured_tool_errors_are_truncated_as_valid_json():
    messages = []
    originals = []
    for index in range(5):
        error_content = json.dumps(
            {
                "ok": False,
                "status": "error",
                "error": {
                    "kind": "shell_command_error",
                    "message": f"failure-{index} " * 600,
                    "retryable": False,
                    "stage": "tool_execution",
                },
            }
        )
        originals.append(error_content)
        messages.extend(
            [
                create_mock_message(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': ''}"),
                create_mock_message(MessageRole.TOOL_RESPONSE, error_content),
            ]
        )

    compressed, chars_saved = _apply_tool_output_truncation(messages)

    assert chars_saved > 0
    for response_index in (1, 3, 5, 7):
        content = _extract_content_text(compressed[response_index].message.content)
        payload = json.loads(content)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "shell_command_error"
        assert "Truncated" in payload["error"]["message"]
        assert len(content) <= 2000
    assert _extract_content_text(compressed[9].message.content) == originals[4]


def test_old_structured_tool_error_with_oversized_metadata_stays_valid_json():
    oversized_error = json.dumps(
        {
            "ok": False,
            "status": "error",
            "error": {
                "kind": "K" * 2500,
                "message": "boom",
                "retryable": False,
                "stage": "tool_execution",
            },
        }
    )
    newest_error = json.dumps(
        {
            "ok": False,
            "status": "error",
            "error": {
                "kind": "execution_error",
                "message": "newest",
                "retryable": False,
                "stage": "tool_execution",
            },
        }
    )
    messages = [
        create_mock_message(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': 'old'}"),
        create_mock_message(MessageRole.TOOL_RESPONSE, oversized_error),
        create_mock_message(MessageRole.TOOL_CALL, "{'name': 'shell_tool', 'arguments': 'new'}"),
        create_mock_message(MessageRole.TOOL_RESPONSE, newest_error),
    ]

    compressed, chars_saved = _apply_tool_output_truncation(messages)
    preserved = _extract_content_text(compressed[1].message.content)

    assert chars_saved == 0
    assert preserved == oversized_error
    assert json.loads(preserved)["error"]["kind"] == "K" * 2500


def test_smart_summary_keeps_two_recent_user_turns_verbatim(monkeypatch):
    captured = {}

    class SummaryModel:
        def generate(self, messages):
            captured["request"] = messages
            return ChatMessage(role=MessageRole.ASSISTANT, content="summary of old work")

    monkeypatch.setattr(
        compression_module.model_manager,
        "get_smolagents_model",
        lambda _model_type: SummaryModel(),
    )
    messages = to_internal_messages(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="OLD USER TURN"),
            ChatMessage(role=MessageRole.ASSISTANT, content="OLD ASSISTANT TURN"),
            ChatMessage(role=MessageRole.USER, content="RECENT USER ONE"),
            ChatMessage(role=MessageRole.ASSISTANT, content="RECENT ASSISTANT ONE"),
            ChatMessage(role=MessageRole.USER, content="RECENT USER TWO"),
            ChatMessage(role=MessageRole.ASSISTANT, content="RECENT ASSISTANT TWO"),
        ]
    )

    result = summarize_conversation(messages, model_id="dummy-model")

    assert result.error is None
    request_text = "\n".join(_extract_content_text(message.content) for message in captured["request"])
    assert "OLD USER TURN" in request_text
    assert "RECENT USER ONE" not in request_text
    visible_texts = [
        _extract_content_text(message.content)
        for message in to_api_messages(result.messages)
    ]
    assert visible_texts == [
        "system",
        "## Conversation Summary\nsummary of old work",
        "RECENT USER ONE",
        "RECENT ASSISTANT ONE",
        "RECENT USER TWO",
        "RECENT ASSISTANT TWO",
    ]


def test_smart_summary_preprocessing_keeps_two_recent_turns_byte_exact(monkeypatch):
    captured = {}
    recent_one = "recent tool output one " * 300
    recent_two = "recent tool output two " * 300
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="system"),
        ChatMessage(role=MessageRole.USER, content="OLD USER"),
        ChatMessage(role=MessageRole.ASSISTANT, content="old answer"),
        ChatMessage(role=MessageRole.USER, content="RECENT USER ONE"),
        ChatMessage(role=MessageRole.TOOL_CALL, content="{'name': 'shell_tool', 'arguments': 'one'}"),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content=recent_one),
        ChatMessage(role=MessageRole.ASSISTANT, content="RECENT ASSISTANT ONE"),
        ChatMessage(role=MessageRole.USER, content="RECENT USER TWO"),
        ChatMessage(role=MessageRole.TOOL_CALL, content="{'name': 'shell_tool', 'arguments': 'two'}"),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content=recent_two),
        ChatMessage(role=MessageRole.ASSISTANT, content="RECENT ASSISTANT TWO"),
    ]

    def capture_summary(messages, **kwargs):
        captured["before_summary"] = messages
        return SummarizeResponse(messages=messages, summary="", error="stop after capture")

    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda current, _model_id: sum(len(_extract_content_text(message.content)) for message in current),
    )
    monkeypatch.setattr(compression_module, "summarize_conversation", capture_summary)
    monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", lambda *args, **kwargs: None)

    manager = ConversationHistoryManager(max_tokens=100, smart_summary=True)
    manager.sync_from_messages(messages)
    manager.get_compressed_messages(model_id="dummy-model")

    before_summary = to_api_messages(captured["before_summary"])
    assert _extract_content_text(before_summary[5].content) == recent_one
    assert _extract_content_text(before_summary[9].content) == recent_two


def test_standard_preprocessing_keeps_two_recent_turns_byte_exact(monkeypatch):
    recent_one = "recent tool output one " * 300
    recent_two = "recent tool output two " * 300
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="system"),
        ChatMessage(role=MessageRole.USER, content="OLD USER"),
        ChatMessage(role=MessageRole.ASSISTANT, content="old answer"),
        ChatMessage(role=MessageRole.USER, content="RECENT USER ONE"),
        ChatMessage(role=MessageRole.TOOL_CALL, content="{'name': 'shell_tool', 'arguments': 'one'}"),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content=recent_one),
        ChatMessage(role=MessageRole.ASSISTANT, content="RECENT ASSISTANT ONE"),
        ChatMessage(role=MessageRole.USER, content="RECENT USER TWO"),
        ChatMessage(role=MessageRole.TOOL_CALL, content="{'name': 'shell_tool', 'arguments': 'two'}"),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content=recent_two),
        ChatMessage(role=MessageRole.ASSISTANT, content="RECENT ASSISTANT TWO"),
    ]

    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda current, _model_id: sum(len(_extract_content_text(message.content)) for message in current),
    )
    monkeypatch.setattr(ConversationHistoryManager, "truncate_until_fits", lambda *args, **kwargs: None)

    manager = ConversationHistoryManager(max_tokens=100, smart_summary=False)
    manager.sync_from_messages(messages)
    manager.get_compressed_messages(model_id="dummy-model")

    before_fallback = to_api_messages(manager.get_internal_messages())
    assert _extract_content_text(before_fallback[5].content) == recent_one
    assert _extract_content_text(before_fallback[9].content) == recent_two


def test_smart_summary_does_not_run_until_more_than_two_user_turns(monkeypatch):
    monkeypatch.setattr(
        compression_module.model_manager,
        "get_smolagents_model",
        lambda _model_type: (_ for _ in ()).throw(AssertionError("summary model must not run")),
    )
    messages = to_internal_messages(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="USER ONE"),
            ChatMessage(role=MessageRole.ASSISTANT, content="ASSISTANT ONE"),
            ChatMessage(role=MessageRole.USER, content="USER TWO"),
            ChatMessage(role=MessageRole.ASSISTANT, content="ASSISTANT TWO"),
        ]
    )

    result = summarize_conversation(messages, model_id="dummy-model")

    assert result.error == "Not enough messages available for compression"
    assert to_api_messages(result.messages) == [message.message for message in messages]


def test_recent_tail_shrinks_to_token_budget_without_splitting_turn(monkeypatch):
    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda messages, _model_id: sum(len(_extract_content_text(message.content)) for message in messages),
    )
    messages = to_internal_messages(
        [
            ChatMessage(role=MessageRole.USER, content="OLD"),
            ChatMessage(role=MessageRole.ASSISTANT, content="old answer"),
            ChatMessage(role=MessageRole.USER, content="RECENT ONE"),
            ChatMessage(role=MessageRole.ASSISTANT, content="x" * 200),
            ChatMessage(role=MessageRole.USER, content="RECENT TWO"),
            ChatMessage(role=MessageRole.ASSISTANT, content="short"),
        ]
    )

    head, tail = _split_summary_head_and_recent_tail(
        messages,
        model_id="dummy-model",
        preserve_recent_tokens=50,
    )

    assert [_extract_content_text(message.message.content) for message in head][-2:] == [
        "RECENT ONE",
        "x" * 200,
    ]
    assert [_extract_content_text(message.message.content) for message in tail] == [
        "RECENT TWO",
        "short",
    ]


def test_recent_tail_is_omitted_when_one_complete_turn_exceeds_budget(monkeypatch):
    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda messages, _model_id: sum(len(_extract_content_text(message.content)) for message in messages),
    )
    messages = to_internal_messages(
        [
            ChatMessage(role=MessageRole.USER, content="OLD"),
            ChatMessage(role=MessageRole.ASSISTANT, content="old answer"),
            ChatMessage(role=MessageRole.USER, content="RECENT"),
            ChatMessage(role=MessageRole.ASSISTANT, content="x" * 200),
        ]
    )

    head, tail = _split_summary_head_and_recent_tail(
        messages,
        model_id="dummy-model",
        preserve_recent_tokens=50,
    )

    assert head == messages
    assert tail == []


def test_recent_tail_falls_back_to_assistant_suffix_within_oversized_turn(monkeypatch):
    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda messages, _model_id: sum(len(_extract_content_text(message.content)) for message in messages),
    )
    messages = to_internal_messages(
        [
            ChatMessage(role=MessageRole.USER, content="OLD"),
            ChatMessage(role=MessageRole.ASSISTANT, content="old answer"),
            ChatMessage(role=MessageRole.USER, content="x" * 200),
            ChatMessage(role=MessageRole.ASSISTANT, content="compact continuation"),
        ]
    )

    head, tail = _split_summary_head_and_recent_tail(
        messages,
        model_id="dummy-model",
        preserve_recent_tokens=50,
    )

    assert head == messages[:-1]
    assert tail == messages[-1:]


def test_summary_accepts_single_oversized_head_when_assistant_suffix_fits(monkeypatch):
    calls = []

    class SummaryModel:
        def generate(self, messages):
            calls.append(messages)
            return ChatMessage(role=MessageRole.ASSISTANT, content="oversized request summary")

    monkeypatch.setattr(
        compression_module,
        "_count_tokens",
        lambda messages, _model_id: sum(len(_extract_content_text(message.content)) for message in messages),
    )
    monkeypatch.setattr(
        compression_module.model_manager,
        "get_smolagents_model",
        lambda _model_type: SummaryModel(),
    )
    messages = to_internal_messages(
        [
            ChatMessage(role=MessageRole.SYSTEM, content="system"),
            ChatMessage(role=MessageRole.USER, content="x" * 200),
            ChatMessage(role=MessageRole.ASSISTANT, content="compact continuation"),
        ]
    )

    result = summarize_conversation(
        messages,
        model_id="dummy-model",
        preserve_recent_tokens=50,
    )

    assert result.error is None
    assert len(calls) == 1
    assert [_extract_content_text(message.content) for message in to_api_messages(result.messages)] == [
        "system",
        "## Conversation Summary\noversized request summary",
        "compact continuation",
    ]
