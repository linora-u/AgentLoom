import copy
from pathlib import Path

import pytest
import yaml

import src.lib.smolagents.agent.yaml_agent_factory as yaml_agent_factory
from src.lib.logging import initialize_global_logger_once, get_global_logger, set_global_logger
from src.lib.smolagents.agent.yaml_agent_factory import (
    YamlAgentFactory,
    YamlConfiguredAgent,
    YamlConfiguredSupervisorAgent,
)


@pytest.fixture(autouse=True)
def _ensure_global_logger():
    """Ensure a global logger exists for agent construction."""
    prev = get_global_logger(create_if_missing=False)
    if prev is None:
        initialize_global_logger_once("test_yaml_tool_defaults")
    yield
    if prev is None:
        set_global_logger(prev)

WORKFLOW_INTRO = yaml_agent_factory.WORKFLOW_EXECUTION_INTRO
WORKFLOW_GUIDANCE = yaml_agent_factory.TASK_SPEC_WORKFLOW_GUIDANCE


def _make_worker(config: dict) -> YamlConfiguredAgent:
    worker = object.__new__(YamlConfiguredAgent)
    worker._config = config
    worker._normalized = None
    worker.run = lambda q, additional_args=None: f"RUN::{q}"
    worker.process_tool_query = lambda q: q
    return worker


def _basic_schema(description: str = "callable tool doc") -> dict:
    return {
        "description": description,
        "inputs": {
            "query": {
                "description": "query text",
                "required": True,
            }
        },
        "output": {
            "description": "result text",
        },
    }


def _write_prompt_protocol_yaml(path, prompt_protocol: dict) -> None:
    with path.open("w", encoding="utf-8") as fp:
        yaml.safe_dump({"prompt_protocol": prompt_protocol}, fp, sort_keys=False, allow_unicode=True)


def test_prompt_protocol_constants_loaded_from_yaml_file():
    protocol = yaml_agent_factory._load_prompt_protocol_config(yaml_agent_factory._PROMPT_PROTOCOL_PATH)

    assert yaml_agent_factory.TASK_SPEC_SECTION_HEADER == protocol["task_spec_section_header"]
    assert yaml_agent_factory.TASK_SPEC_SECTION_GUIDANCE_BASE == protocol["task_spec_section_guidance_base"]
    assert yaml_agent_factory.FINAL_BRIDGE_INSTRUCTION == protocol["final_bridge_instruction"]
    assert yaml_agent_factory.WORKFLOW_EXECUTION_INTRO == protocol["workflow_execution_intro"]
    assert yaml_agent_factory.WORKFLOW_OUTER_INDENT == protocol["workflow_outer_indent"]
    assert yaml_agent_factory.WORKFLOW_INNER_INDENT == protocol["workflow_inner_indent"]
    assert yaml_agent_factory.OUTPUT_RULE_LINES == tuple(protocol["output_rule_lines"])


def test_prompt_protocol_loader_raises_when_file_missing(tmp_path):
    missing_yaml = tmp_path / "missing_prompt_protocol.yaml"

    with pytest.raises(RuntimeError, match="Prompt protocol config file not found"):
        yaml_agent_factory._load_prompt_protocol_config(missing_yaml)


def test_prompt_protocol_loader_raises_when_required_key_missing(tmp_path):
    protocol = copy.deepcopy(yaml_agent_factory._PROMPT_PROTOCOL)
    protocol.pop("task_spec_section_header")
    invalid_yaml = tmp_path / "missing_key_prompt_protocol.yaml"
    _write_prompt_protocol_yaml(invalid_yaml, protocol)

    with pytest.raises(ValueError, match="missing required fields"):
        yaml_agent_factory._load_prompt_protocol_config(invalid_yaml)


def test_prompt_protocol_loader_raises_when_output_rule_lines_invalid(tmp_path):
    protocol = copy.deepcopy(yaml_agent_factory._PROMPT_PROTOCOL)
    protocol["output_rule_lines"] = ["ok", 2]
    invalid_yaml = tmp_path / "invalid_output_rules_prompt_protocol.yaml"
    _write_prompt_protocol_yaml(invalid_yaml, protocol)

    with pytest.raises(ValueError, match="output_rule_lines"):
        yaml_agent_factory._load_prompt_protocol_config(invalid_yaml)


def test_prompt_protocol_loader_expands_variables(tmp_path):
    protocol = copy.deepcopy(yaml_agent_factory._PROMPT_PROTOCOL)
    protocol["variables"] = {"task_tag": "<task_spec>"}
    protocol["task_spec_section_guidance_tail"] = "Follow ${task_tag} first."
    protocol["output_rule_lines"] = ["1. Return ${task_tag} result."]
    valid_yaml = tmp_path / "expanded_variables_prompt_protocol.yaml"
    _write_prompt_protocol_yaml(valid_yaml, protocol)

    loaded = yaml_agent_factory._load_prompt_protocol_config(valid_yaml)
    assert loaded["task_spec_section_guidance_tail"] == "Follow <task_spec> first."
    assert loaded["output_rule_lines"] == ["1. Return <task_spec> result."]


def test_prompt_protocol_loader_raises_when_variable_undefined(tmp_path):
    protocol = copy.deepcopy(yaml_agent_factory._PROMPT_PROTOCOL)
    protocol["variables"] = {"task_tag": "<task_spec>"}
    protocol["task_spec_section_guidance_tail"] = "Follow ${missing_tag} first."
    invalid_yaml = tmp_path / "undefined_variable_prompt_protocol.yaml"
    _write_prompt_protocol_yaml(invalid_yaml, protocol)

    with pytest.raises(ValueError, match="undefined variable"):
        yaml_agent_factory._load_prompt_protocol_config(invalid_yaml)


def test_agent_as_tool_exports_when_agent_function_schema_present():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_schema": _basic_schema(),
        }
    )

    worker._validate_config()
    tool = worker.agent_as_tool()

    assert tool is not None
    assert tool.__name__ == "demo_worker"
    assert "Args:" in (tool.__doc__ or "")
    assert "Returns:" in (tool.__doc__ or "")
    out = tool("hello")
    assert out.startswith("RUN::Task specification (what you must follow in this task):")
    assert "Task inputs (what you should process for this specific call):" in out
    assert "Expected output (what result you must produce):" in out
    assert "<task_spec>" in out
    assert "</task_spec>" in out
    assert "<inputs>" in out
    assert "<output>" in out
    assert "result text" in out
    assert "<workflow>" not in out
    assert WORKFLOW_INTRO not in out
    assert WORKFLOW_GUIDANCE not in out
    assert out.index("<task_spec>") < out.index("<inputs>") < out.index("<output>")
    assert "1. query text: hello" in out


def test_agent_as_tool_not_exported_when_agent_function_schema_missing():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
        }
    )

    worker._validate_config()
    assert worker.agent_as_tool() is None


def test_legacy_agent_function_description_is_ignored_without_schema():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_description": "legacy description",
        }
    )

    worker._validate_config()
    assert worker.agent_as_tool() is None


def test_validate_config_rejects_invalid_agent_function_schema_type():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_schema": "invalid",
        }
    )

    with pytest.raises(ValueError, match="agent_function_schema"):
        worker._validate_config()


def test_validate_config_rejects_missing_input_description():
    schema = _basic_schema()
    schema["inputs"]["query"].pop("description")

    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_schema": schema,
        }
    )

    with pytest.raises(ValueError, match="description"):
        worker._validate_config()


def test_validate_config_ignores_input_type_when_present():
    schema = _basic_schema()
    schema["inputs"]["query"]["type"] = "integer"

    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_schema": schema,
        }
    )

    worker._normalized = worker._validate_config()
    normalized = worker._normalized.agent_function_schema
    assert normalized is not None
    assert normalized["inputs"]["query"]["type"] == "string"


def test_validate_config_rejects_missing_output_description():
    schema = _basic_schema()
    schema["output"] = {}

    worker = _make_worker(
        {
            "name": "demo_worker",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_schema": schema,
        }
    )

    with pytest.raises(ValueError, match=r"output\.description"):
        worker._validate_config()


def test_factory_create_agent_as_tool_exports_when_schema_present():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
        "agent_function_schema": _basic_schema("factory tool doc"),
    }
    tool = YamlAgentFactory.create_agent_as_tool(config, model=object())
    assert tool is not None
    assert tool.__name__ == "demo_worker"


def test_factory_create_agent_as_tool_not_exported_when_schema_missing():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
    }
    tool = YamlAgentFactory.create_agent_as_tool(config, model=object())
    assert tool is None


def test_generated_tool_supports_multiple_inputs_and_optional_fields():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
        "agent_function_schema": {
            "description": "multi input worker",
            "inputs": {
                "query": {"description": "main task", "required": True},
                "tag": {"description": "context tag", "required": False},
            },
            "output": {"description": "worker textual output"},
        },
    }

    worker = _make_worker(config)
    worker._validate_config()

    tool = worker.agent_as_tool()

    out = tool("hello", tag="demo")
    assert out.startswith("RUN::Task specification (what you must follow in this task):")
    assert "<task_spec>" in out
    assert "<inputs>" in out
    assert "<output>" in out
    assert "worker textual output" in out
    assert "<workflow>" not in out
    assert WORKFLOW_INTRO not in out
    assert WORKFLOW_GUIDANCE not in out
    assert out.index("<task_spec>") < out.index("<inputs>") < out.index("<output>")
    assert "1. main task: hello" in out
    assert "2. context tag: demo" in out


def test_generated_tool_embeds_list_workflow_items_without_stage_wrappers():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": [
            "First workflow item.\nUse the provided input.",
            "Second workflow item.\nReturn the final answer.",
        ],
        "agent_function_schema": _basic_schema(),
    }
    worker = _make_worker(config)
    worker._validate_config()

    tool = worker.agent_as_tool()
    out = tool("hello")

    assert "First workflow item.\nUse the provided input." in out
    assert "Second workflow item.\nReturn the final answer." in out
    assert out.index("First workflow item.") < out.index("Second workflow item.")
    assert "Stage 1" not in out
    assert "Workflow item 1" not in out


def test_supervisor_respects_empty_toolsets():
    supervisor = YamlConfiguredSupervisorAgent(
        config={
            "name": "demo",
            "description": "demo supervisor",
            "workflow": "demo workflow",
            "tools": [],
            "worker_agents": [],
            "toolsets": [],
            "_yaml_file_path": str(Path("applications/test_demo/workflows/test_multi_workflow_agent.yaml").resolve()),
        },
        model="dummy",
    )

    assert supervisor._get_tools() == []


def test_generated_tool_embeds_mermaid_block_into_workflow_tag():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": (
            "Intro text.\n\n"
            "```mermaid\n"
            "flowchart TD\n"
            "A-->B\n"
            "```\n\n"
            "Tail text."
        ),
        "agent_function_schema": _basic_schema(),
    }
    worker = _make_worker(config)
    worker._validate_config()

    tool = worker.agent_as_tool()
    out = tool("hello")

    assert "<task_spec>" in out
    assert WORKFLOW_GUIDANCE in out
    assert WORKFLOW_INTRO in out
    assert "<workflow>" in out
    assert "\n  <workflow>\n" in out
    assert "\n    flowchart TD\n" in out
    assert "flowchart TD" in out
    assert "A-->B" in out
    assert out.index("<task_spec>") < out.index("<inputs>") < out.index("<output>")


def test_generated_tool_appends_warning_when_mermaid_invalid(monkeypatch):
    warning_text = "Mermaid syntax validation failed for workflow block #1."
    monkeypatch.setattr(
        yaml_agent_factory,
        "_validate_mermaid_text",
        lambda _code: warning_text,
    )

    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": (
            "```mermaid\n"
            "flowchart TD\n"
            "A-/->B\n"
            "```\n"
        ),
        "agent_function_schema": _basic_schema(),
    }
    worker = _make_worker(config)
    worker._validate_config()

    tool = worker.agent_as_tool()
    out = tool("hello")

    assert "<task_spec>" in out
    assert WORKFLOW_GUIDANCE in out
    assert WORKFLOW_INTRO in out
    assert "<workflow>" in out
    assert "\n  <workflow>\n" in out
    assert "\n    flowchart TD\n" in out
    assert "Workflow validation warnings:" in out
    assert warning_text in out
