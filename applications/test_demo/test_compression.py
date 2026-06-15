#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from smolagents.models import ChatMessage, MessageRole

from src.lib.smolagents.agent.yaml_agent_factory import (
    YamlAgentFactory,
    YamlConfiguredSupervisorAgent,
)
from src.lib.logging import get_global_logger, initialize_global_logger_once
from src.lib.smolagents.memory.context_compression import (
    FILE_DEDUP_PLACEHOLDER,
    OBSERVATION_MASKING_PLACEHOLDER,
    ConversationHistoryManager,
    InternalChatMessage,
    _extract_content_text,
)
from src.lib.smolagents.models.model_manager import model_manager
from src.lib.smolagents.models.model_types import ModelType
from src.trace import generate_id


ScenarioBuilder = Callable[[], list[ChatMessage]]
AgentValidator = Callable[["ScenarioResult"], None]


@dataclass
class ScenarioResult:
    name: str
    api_messages: list[ChatMessage]
    internal_messages: list[InternalChatMessage]
    summary_count: int
    truncation_marker_count: int


@dataclass
class AgentScenario:
    name: str
    task: str
    validator: AgentValidator


def _msg(role: MessageRole, text: str) -> ChatMessage:
    return ChatMessage(role=role, content=[{"type": "text", "text": text}])


def _python_call(source: str) -> ChatMessage:
    return _msg(
        MessageRole.TOOL_CALL,
        "{'name': 'python_interpreter', 'arguments': " + repr(source) + "}",
    )


def _large_text(label: str, repeats: int = 1200) -> str:
    return (f"{label} compact functional-test payload. " * repeats).strip()


def _text(messages: Iterable[ChatMessage | InternalChatMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        chat_message = message.message if isinstance(message, InternalChatMessage) else message
        parts.append(_extract_content_text(chat_message.content))
    return "\n".join(parts)


def _model_id_for_type(model_type: ModelType) -> str:
    config = model_manager.get_model_config(model_type)
    if not config.model_id:
        raise RuntimeError(f"{model_type.value} model is not configured")
    return config.model_id


def _summary_model_id() -> str:
    return _model_id_for_type(ModelType.SUMMARY)


def _assert_summary_config_usable() -> None:
    summary = model_manager.get_model_config(ModelType.SUMMARY)
    if not summary.model_id:
        raise AssertionError("summary model is not configured")
    if not summary.api_key:
        raise AssertionError("summary api_key is not configured")
    if summary.base_url and not summary.base_url.startswith(("http://", "https://")):
        raise AssertionError(
            "summary base_url must include http:// or https://; "
            f"got {summary.base_url!r}"
        )


def _ensure_global_logger() -> None:
    if get_global_logger(create_if_missing=False) is None:
        initialize_global_logger_once("compression_test")


@contextlib.contextmanager
def _force_history_max_tokens(max_tokens: int):
    original_init = ConversationHistoryManager.__init__

    def patched_init(self, max_tokens: int | None = None, smart_summary: bool = True):
        return original_init(self, max_tokens=max_tokens_arg, smart_summary=smart_summary)

    max_tokens_arg = max_tokens
    ConversationHistoryManager.__init__ = patched_init
    try:
        yield
    finally:
        ConversationHistoryManager.__init__ = original_init


def _assert_valid_tool_pair_structure(messages: list[ChatMessage]) -> None:
    for idx, message in enumerate(messages):
        if message.role == MessageRole.TOOL_RESPONSE:
            if idx == 0 or messages[idx - 1].role != MessageRole.TOOL_CALL:
                raise AssertionError(f"orphan TOOL_RESPONSE at API message index {idx}")
        if message.role == MessageRole.TOOL_CALL:
            if idx + 1 >= len(messages) or messages[idx + 1].role != MessageRole.TOOL_RESPONSE:
                raise AssertionError(f"orphan TOOL_CALL at API message index {idx}")


def _count_role(messages: Iterable[InternalChatMessage], role: MessageRole) -> int:
    return sum(1 for message in messages if message.message.role == role)


def _assert_compaction_happened(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    has_compaction_signal = (
        result.summary_count > 0
        or result.truncation_marker_count > 0
        or FILE_DEDUP_PLACEHOLDER in internal_text
        or OBSERVATION_MASKING_PLACEHOLDER in internal_text
        or "Truncated" in internal_text
    )
    if not has_compaction_signal:
        raise AssertionError(f"{result.name}: compression was not triggered")


def _assert_visible_summary_survived(result: ScenarioResult) -> None:
    if "## Conversation Summary" not in _text(result.api_messages):
        raise AssertionError(f"{result.name}: final API messages lost the visible summary")


def _assert_real_summary_used(result: ScenarioResult) -> None:
    if result.summary_count <= 0:
        raise AssertionError(f"{result.name}: real summary model was not called")


def _run_history_scenario(
    name: str,
    history: list[ChatMessage],
    max_tokens: int,
) -> ScenarioResult:
    manager = ConversationHistoryManager(max_tokens=max_tokens, smart_summary=True)
    manager.sync_from_messages(history)

    api_messages = manager.get_compressed_messages(model_id=_summary_model_id())
    _assert_valid_tool_pair_structure(api_messages)

    internal_messages = manager.get_internal_messages()
    result = ScenarioResult(
        name=name,
        api_messages=api_messages,
        internal_messages=internal_messages,
        summary_count=sum(1 for message in internal_messages if message.is_summary),
        truncation_marker_count=sum(1 for message in internal_messages if message.is_truncation_marker),
    )
    _assert_compaction_happened(result)
    _assert_real_summary_used(result)
    _assert_visible_summary_survived(result)
    return result


def _build_repeated_read_history() -> list[ChatMessage]:
    target = PROJECT_ROOT / "applications" / "test_demo" / "test_compression.py"
    same_range = f'content = read_file(file_path="{target}", offset=1, limit=80)\nprint(content)'
    later_range = f'content = read_file(file_path="{target}", offset=81, limit=80)\nprint(content)'

    return [
        _msg(MessageRole.SYSTEM, "system prompt for compact functional test"),
        _msg(MessageRole.USER, _large_text("repeated-read-input")),
        _python_call(same_range),
        _msg(MessageRole.TOOL_RESPONSE, "same read old content " * 40),
        _msg(MessageRole.ASSISTANT, "I will verify the same range again."),
        _python_call(same_range),
        _msg(MessageRole.TOOL_RESPONSE, "same read newest content " * 40),
        _python_call(later_range),
        _msg(MessageRole.TOOL_RESPONSE, "different range content " * 50),
        _msg(MessageRole.ASSISTANT, _large_text("repeated-read-analysis", repeats=900)),
    ]


def _build_large_output_history() -> list[ChatMessage]:
    large_output = "\n".join(
        f"LARGE_OUTPUT_SENTINEL_{line:04d} " + ("x" * 80)
        for line in range(800)
    )
    return [
        _msg(MessageRole.SYSTEM, "system prompt for compact functional test"),
        _msg(MessageRole.USER, _large_text("large-output-input")),
        _python_call('result = shell_tool(commands=["python -c \\"print(\\\'x\\\' * 90000)\\""])\nprint(result)'),
        _msg(MessageRole.TOOL_RESPONSE, large_output),
        _msg(MessageRole.ASSISTANT, _large_text("large-output-analysis", repeats=900)),
    ]


def _build_skill_load_history() -> list[ChatMessage]:
    return [
        _msg(MessageRole.SYSTEM, "system prompt for compact functional test"),
        _msg(MessageRole.USER, _large_text("skill-load-input")),
        _msg(
            MessageRole.TOOL_CALL,
            "{'name': 'load_skill', 'arguments': {'skill': 'agentloom-framework-skill'}}",
        ),
        _msg(MessageRole.TOOL_RESPONSE, "SKILL_LOAD_SENTINEL " * 1000),
        _python_call('result = shell_tool(commands=["printf old-output"])\nprint(result)'),
        _msg(MessageRole.TOOL_RESPONSE, "ordinary old output " * 1000),
        _msg(MessageRole.ASSISTANT, _large_text("skill-load-analysis", repeats=900)),
    ]


def _build_error_recovery_history() -> list[ChatMessage]:
    return [
        _msg(MessageRole.SYSTEM, "system prompt for compact functional test"),
        _msg(MessageRole.USER, _large_text("error-recovery-input")),
        _python_call('result = shell_tool(commands=["printf old-output"])\nprint(result)'),
        _msg(MessageRole.TOOL_RESPONSE, "OLD_OUTPUT_SENTINEL " * 2000),
        _msg(MessageRole.ASSISTANT, "The first command was too broad; I will try a narrower command."),
        _python_call('content = read_file(file_path="/tmp/definitely-missing.txt")\nprint(content)'),
        _msg(MessageRole.TOOL_RESPONSE, "Error: file does not exist: /tmp/definitely-missing.txt ERROR_RECOVERY_SENTINEL"),
        _msg(MessageRole.ASSISTANT, _large_text("error-recovery-analysis", repeats=900)),
    ]


SCENARIOS: dict[str, ScenarioBuilder] = {
    "repeated-read": _build_repeated_read_history,
    "large-output": _build_large_output_history,
    "skill-load": _build_skill_load_history,
    "error-recovery": _build_error_recovery_history,
}


def _validate_repeated_read(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    if FILE_DEDUP_PLACEHOLDER not in internal_text:
        raise AssertionError("repeated-read: old duplicate file response was not deduped")
    if "same read newest content" not in internal_text:
        raise AssertionError("repeated-read: latest repeated read was not preserved")
    if "different range content" not in internal_text:
        raise AssertionError("repeated-read: different range was incorrectly removed")


def _validate_large_output(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    api_text = _text(result.api_messages)
    if "Truncated" not in internal_text:
        raise AssertionError("large-output: Layer 2 truncation marker was not produced")
    if api_text.count("LARGE_OUTPUT_SENTINEL_") > 20:
        raise AssertionError("large-output: final API messages still carry full giant output")


def _validate_skill_load(result: ScenarioResult) -> None:
    for idx, internal_message in enumerate(result.internal_messages[:-1]):
        call_text = _extract_content_text(internal_message.message.content)
        if "load_skill" not in call_text:
            continue
        response = result.internal_messages[idx + 1]
        response_text = _extract_content_text(response.message.content)
        if response_text == OBSERVATION_MASKING_PLACEHOLDER:
            raise AssertionError("skill-load: skill load response was masked")
    if "SKILL_LOAD_SENTINEL" not in _text(result.internal_messages):
        raise AssertionError("skill-load: recent skill load content disappeared")


def _validate_error_recovery(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    if "ERROR_RECOVERY_SENTINEL" not in internal_text:
        raise AssertionError("error-recovery: recent Error tool response disappeared")
    if _text(result.api_messages).count("OLD_OUTPUT_SENTINEL") > 20:
        raise AssertionError("error-recovery: old ordinary tool response was not compressed")


VALIDATORS: dict[str, Callable[[ScenarioResult], None]] = {
    "repeated-read": _validate_repeated_read,
    "large-output": _validate_large_output,
    "skill-load": _validate_skill_load,
    "error-recovery": _validate_error_recovery,
}


def _agent_task(title: str, code: str) -> str:
    return f"""
Run the compact application scenario: {title}.
Execute the Python tool block below. After it runs, provide a short final answer.
Do not call tools outside this block.

Python tool block:
{code.strip()}
"""


def _build_agent_scenarios() -> dict[str, AgentScenario]:
    demo_dir = PROJECT_ROOT / "applications" / "test_demo"
    target = demo_dir / "test_compression.py"
    workflow = demo_dir / "workflows" / "compression_test_agent.yaml"

    repeated_read_code = f"""
dir_result = browse_directory(directory_path={str(demo_dir)!r}, max_depth=2, show_file_info=True)
first_read = read_file(file_path={str(target)!r}, offset=1, limit=120)
second_read = read_file(file_path={str(target)!r}, offset=1, limit=120)
third_read = read_file(file_path={str(target)!r}, offset=121, limit=80)
search_result = grep_search(pattern="ConversationHistoryManager", path={str(target)!r}, max_results=20)
print("APP_REPEATED_READ_SENTINEL")
print(dir_result)
print(first_read)
print(second_read)
print(third_read)
print(search_result)
"""

    large_output_code = f"""
large_output = shell_tool(command="yes APP_LARGE_OUTPUT_SENTINEL_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx | head -n 1200", timeout=60)
glob_result = glob_search(pattern="**/*.py", path={str(PROJECT_ROOT / "src" / "lib" / "smolagents")!r}, max_results=120)
print("APP_LARGE_OUTPUT_START")
print(large_output)
print(glob_result)
"""

    search_mix_code = f"""
glob_result = glob_search(pattern="workflows/*.yaml", path={str(demo_dir)!r}, max_results=80, sort_by="name")
grep_result = grep_search(pattern="model_type|ConversationHistoryManager|smart_summary", path={str(demo_dir)!r}, include="*.py", max_results=100, context_lines=2)
workflow_read = read_file(file_path={str(workflow)!r}, offset=1, limit=120)
print("APP_SEARCH_MIX_SENTINEL")
print(glob_result)
print(grep_result)
print(workflow_read)
"""

    missing_path = demo_dir / "definitely_missing_compact_file.txt"
    error_recovery_code = f"""
before = shell_tool(command="printf APP_ERROR_RECOVERY_BEFORE", timeout=30)
missing = shell_tool(command="ls {str(missing_path)!r}", timeout=30)
after = grep_search(pattern="def _run_history_scenario", path={str(target)!r}, max_results=10)
print("APP_ERROR_RECOVERY_SENTINEL")
print(before)
print(missing)
print(after)
"""

    return {
        "agent-repeated-read": AgentScenario(
            name="agent-repeated-read",
            task=_agent_task("repeated file reads plus grep", repeated_read_code),
            validator=_validate_agent_repeated_read,
        ),
        "agent-large-output": AgentScenario(
            name="agent-large-output",
            task=_agent_task("large shell output plus glob", large_output_code),
            validator=_validate_agent_large_output,
        ),
        "agent-search-mix": AgentScenario(
            name="agent-search-mix",
            task=_agent_task("glob, grep, and workflow read", search_mix_code),
            validator=_validate_agent_search_mix,
        ),
        "agent-error-recovery": AgentScenario(
            name="agent-error-recovery",
            task=_agent_task("failed command followed by recovery search", error_recovery_code),
            validator=_validate_agent_error_recovery,
        ),
    }


def _validate_agent_repeated_read(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    if "APP_REPEATED_READ_SENTINEL" not in internal_text:
        raise AssertionError("agent-repeated-read: required tool block did not execute")
    if FILE_DEDUP_PLACEHOLDER not in internal_text:
        raise AssertionError("agent-repeated-read: repeated read was not deduped")


def _validate_agent_large_output(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    api_text = _text(result.api_messages)
    if "APP_LARGE_OUTPUT_START" not in internal_text:
        raise AssertionError("agent-large-output: required tool block did not execute")
    if "Truncated" not in internal_text and OBSERVATION_MASKING_PLACEHOLDER not in internal_text:
        raise AssertionError("agent-large-output: large output was not compacted")
    if api_text.count("APP_LARGE_OUTPUT_SENTINEL") > 20:
        raise AssertionError("agent-large-output: final API messages still carry the large output")


def _validate_agent_search_mix(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    if "APP_SEARCH_MIX_SENTINEL" not in internal_text:
        raise AssertionError("agent-search-mix: required tool block did not execute")
    if "compression_test_agent.yaml" not in internal_text:
        raise AssertionError("agent-search-mix: workflow read/search evidence disappeared")


def _validate_agent_error_recovery(result: ScenarioResult) -> None:
    internal_text = _text(result.internal_messages)
    if "APP_ERROR_RECOVERY_SENTINEL" not in internal_text:
        raise AssertionError("agent-error-recovery: required tool block did not execute")
    if "definitely_missing_compact_file" not in internal_text:
        raise AssertionError("agent-error-recovery: failed command evidence disappeared")
    if "def _run_history_scenario" not in internal_text:
        raise AssertionError("agent-error-recovery: recovery search evidence disappeared")


def _run_consecutive_compact(max_tokens: int) -> ScenarioResult:
    first = _build_large_output_history()
    second = [
        _msg(MessageRole.USER, _large_text("consecutive-second-input")),
        _python_call('result = shell_tool(commands=["printf second-run"])\nprint(result)'),
        _msg(MessageRole.TOOL_RESPONSE, "SECOND_RUN_SENTINEL " * 1500),
        _msg(MessageRole.ASSISTANT, _large_text("consecutive-second-analysis", repeats=900)),
    ]

    manager = ConversationHistoryManager(max_tokens=max_tokens, smart_summary=True)
    manager.sync_from_messages(first)
    first_api = manager.get_compressed_messages(model_id=_summary_model_id())
    _assert_valid_tool_pair_structure(first_api)
    first_summary_count = sum(1 for message in manager.get_internal_messages() if message.is_summary)
    if first_summary_count <= 0:
        raise AssertionError("consecutive-compact: first compact did not call real summary model")

    manager.sync_from_messages(first + second)
    api_messages = manager.get_compressed_messages(model_id=_summary_model_id())
    _assert_valid_tool_pair_structure(api_messages)
    internal_messages = manager.get_internal_messages()
    second_summary_count = sum(1 for message in internal_messages if message.is_summary)
    if second_summary_count <= first_summary_count:
        raise AssertionError("consecutive-compact: second compact did not create a new summary")

    visible_summary_count = sum(1 for message in internal_messages if message.is_summary and message.is_visible())
    if visible_summary_count > 1:
        raise AssertionError(
            f"consecutive-compact: expected at most one visible summary, got {visible_summary_count}"
        )
    archived_summary_count = sum(
        1 for message in internal_messages
        if message.is_summary and (message.condense_id or message.truncation_parent)
    )
    if archived_summary_count <= 0:
        raise AssertionError("consecutive-compact: previous summary was not archived by the second compact")

    result = ScenarioResult(
        name="consecutive-compact",
        api_messages=api_messages,
        internal_messages=internal_messages,
        summary_count=second_summary_count,
        truncation_marker_count=sum(1 for message in internal_messages if message.is_truncation_marker),
    )
    _assert_compaction_happened(result)
    _assert_visible_summary_survived(result)
    return result


def _run_agent_application_scenario(
    scenario: AgentScenario,
    max_tokens: int,
    max_steps: int,
) -> ScenarioResult:
    _ensure_global_logger()
    yaml_path = PROJECT_ROOT / "applications" / "test_demo" / "workflows" / "compression_test_agent.yaml"
    config = YamlAgentFactory._load_config_from_file(yaml_path)
    config["max_steps"] = max_steps
    config["smart_summary"] = True

    with _force_history_max_tokens(max_tokens):
        supervisor = YamlConfiguredSupervisorAgent(config=config)
        run_result = supervisor.run(scenario.task, task_id=generate_id(scenario.task, prefix="task"))

    runtime_agent = getattr(supervisor, "_runtime_agent", None)
    history_manager = getattr(runtime_agent, "_history_manager", None)
    if history_manager is None:
        raise AssertionError(f"{scenario.name}: runtime history manager was not available")

    runtime_internal_messages = history_manager.get_internal_messages()
    runtime_result = ScenarioResult(
        name=scenario.name,
        api_messages=[],
        internal_messages=runtime_internal_messages,
        summary_count=sum(1 for message in runtime_internal_messages if message.is_summary),
        truncation_marker_count=sum(1 for message in runtime_internal_messages if message.is_truncation_marker),
    )
    _assert_compaction_happened(runtime_result)
    _assert_real_summary_used(runtime_result)

    model_id = getattr(getattr(runtime_agent, "model", None), "model_id", None) or _model_id_for_type(ModelType.POWERFUL)
    api_messages = history_manager.get_compressed_messages(model_id=model_id)
    _assert_valid_tool_pair_structure(api_messages)

    internal_messages = history_manager.get_internal_messages()
    result = ScenarioResult(
        name=scenario.name,
        api_messages=api_messages,
        internal_messages=internal_messages,
        summary_count=sum(1 for message in internal_messages if message.is_summary),
        truncation_marker_count=sum(1 for message in internal_messages if message.is_truncation_marker),
    )
    _assert_compaction_happened(result)
    _assert_real_summary_used(result)
    _assert_visible_summary_survived(result)
    if _count_role(internal_messages, MessageRole.TOOL_RESPONSE) <= 0:
        raise AssertionError(f"{scenario.name}: no real tool response was recorded")
    scenario.validator(result)
    if not str(run_result).strip():
        raise AssertionError(f"{scenario.name}: agent returned an empty result")

    print(
        f"{scenario.name}: ok api_messages={len(api_messages)} "
        f"internal_messages={len(internal_messages)} "
        f"summaries={result.summary_count} markers={result.truncation_marker_count} "
        f"tool_responses={_count_role(internal_messages, MessageRole.TOOL_RESPONSE)}"
    )
    return result


def _run_agent_smoke(max_tokens: int, max_steps: int) -> None:
    scenario = _build_agent_scenarios()["agent-repeated-read"]
    _run_agent_application_scenario(scenario, max_tokens=max_tokens, max_steps=max_steps)


def _run_named_scenario(name: str, max_tokens: int) -> ScenarioResult:
    if name == "consecutive-compact":
        result = _run_consecutive_compact(max_tokens=max_tokens)
    else:
        builder = SCENARIOS[name]
        result = _run_history_scenario(
            name=name,
            history=builder(),
            max_tokens=max_tokens,
        )
        VALIDATORS[name](result)

    print(
        f"{name}: ok api_messages={len(result.api_messages)} "
        f"internal_messages={len(result.internal_messages)} "
        f"summaries={result.summary_count} markers={result.truncation_marker_count}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run compact functional tests with real model calls.")
    agent_scenarios = _build_agent_scenarios()
    history_scenario_names = [*SCENARIOS.keys(), "consecutive-compact"]
    agent_scenario_names = list(agent_scenarios.keys())
    parser.add_argument(
        "--scenario",
        choices=[*history_scenario_names, *agent_scenario_names, "history-all", "agent-all", "all"],
        default="repeated-read",
    )
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument(
        "--agent-max-tokens",
        type=int,
        default=7000,
        help="Low compact threshold for real Application scenarios. Keep above the initial system/task prompt.",
    )
    parser.add_argument("--max-steps", type=int, default=2)
    parser.add_argument(
        "--run-agent-smoke",
        action="store_true",
        help="Also run agent-repeated-read through the real Application path.",
    )
    args = parser.parse_args()

    history_names: list[str] = []
    agent_names: list[str] = []

    if args.scenario == "all":
        history_names = history_scenario_names
        agent_names = agent_scenario_names
    elif args.scenario == "history-all":
        history_names = history_scenario_names
    elif args.scenario == "agent-all":
        agent_names = agent_scenario_names
    elif args.scenario in agent_scenarios:
        agent_names = [args.scenario]
    else:
        history_names = [args.scenario]

    _assert_summary_config_usable()

    if args.run_agent_smoke and "agent-repeated-read" not in agent_names:
        agent_names.append("agent-repeated-read")

    for scenario_name in history_names:
        _run_named_scenario(
            scenario_name,
            max_tokens=args.max_tokens,
        )

    for scenario_name in agent_names:
        _run_agent_application_scenario(
            agent_scenarios[scenario_name],
            max_tokens=args.agent_max_tokens,
            max_steps=args.max_steps,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
