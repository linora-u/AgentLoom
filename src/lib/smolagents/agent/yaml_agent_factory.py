import yaml
import re
import json
import inspect
import hashlib
import copy
import threading
from functools import wraps
from typing import Callable, List, Dict, Union, Optional, Any
from pathlib import Path

from src.lib.smolagents import AgentLogger
from src.lib.smolagents.agent.base_agent import AgentRoleProfile, AgentType, RoleDrivenAgent
from src.lib.logging import (
    get_logger,
)
from src.lib.smolagents.agent.agent_validation import AgentConfigNormalizer, NormalizedAgentConfig
from src.lib.utils.dynamic_import import load_function
from src.workflows.workflow_manager import get_worker_agent_yaml_path, infer_category_from_yaml_path
from src.lib.config import C, get_code_agent_config, get_default_tools
from src.tools import resolve_tool_function

# Keep module-level symbol for legacy tests that monkeypatch this path root.
AGENT_ROOT = C.agent_root

# Prompt protocol constants are externalized in prompts/ YAML to keep wording/template
# configuration centralized and editable without changing implementation logic.
_PROMPT_PROTOCOL_PATH = (Path(__file__).resolve().parent.parent / "prompts" / "agent_tool_behavior_spec.yaml").resolve()
_PROMPT_PROTOCOL_REQUIRED_STRING_KEYS = (
    "task_spec_section_header",
    "task_spec_section_guidance_base",
    "task_spec_workflow_guidance",
    "task_spec_section_guidance_tail",
    "task_spec_block_template",
    "workflow_block_template",
    "task_request_section_header",
    "task_request_section_guidance",
    "task_request_block_template",
    "task_spec_warning_header",
    "inputs_section_header",
    "inputs_section_guidance",
    "inputs_list_intro_line",
    "inputs_empty_line",
    "inputs_block_template",
    "output_section_header",
    "output_section_guidance",
    "output_rule_header",
    "output_block_template",
    "final_bridge_instruction",
    "supervisor_bridge_instruction",
    "workflow_execution_intro",
    "workflow_outer_indent",
    "workflow_inner_indent",
)
_PROMPT_PROTOCOL_REQUIRED_KEYS = _PROMPT_PROTOCOL_REQUIRED_STRING_KEYS + ("output_rule_lines",)
_PROMPT_PROTOCOL_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FIXED_ARGS_CONFIG_KEY = "fixed_args"


def _get_fixed_tool_args(tool_config: dict[str, Any]) -> dict[str, Any]:
    raw_fixed_args = tool_config.get(_FIXED_ARGS_CONFIG_KEY)
    if raw_fixed_args is None:
        return {}
    if not isinstance(raw_fixed_args, dict):
        raise ValueError(
            f"Tool '{tool_config.get('name')}' fixed_args must be a dictionary when provided"
        )
    return dict(raw_fixed_args)


def _bind_fixed_tool_args(tool_func: Callable, tool_name: str, fixed_args: dict[str, Any]) -> Callable:
    if not fixed_args:
        return tool_func

    signature = inspect.signature(tool_func)
    parameters = signature.parameters
    accepts_var_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )
    unknown_args = [
        arg_name for arg_name in fixed_args
        if arg_name not in parameters and not accepts_var_kwargs
    ]
    if unknown_args:
        joined_args = ", ".join(sorted(unknown_args))
        raise ValueError(f"Unknown fixed_args for tool '{tool_name}': {joined_args}")

    visible_parameters = [
        parameter for arg_name, parameter in parameters.items()
        if arg_name not in fixed_args
    ]
    visible_signature = signature.replace(parameters=visible_parameters)

    @wraps(tool_func)
    def fixed_args_tool(*args, **kwargs):
        visible_kwargs = dict(kwargs)
        for arg_name in fixed_args:
            visible_kwargs.pop(arg_name, None)
        bound = visible_signature.bind_partial(*args, **visible_kwargs)
        call_kwargs = dict(bound.arguments)
        call_kwargs.update(fixed_args)
        return tool_func(**call_kwargs)

    annotations = dict(getattr(tool_func, "__annotations__", {}))
    for arg_name in fixed_args:
        annotations.pop(arg_name, None)
    fixed_args_tool.__name__ = tool_name
    fixed_args_tool.__qualname__ = tool_name
    fixed_args_tool.__annotations__ = annotations
    fixed_args_tool.__signature__ = visible_signature
    fixed_args_tool._agentloom_fixed_args = tuple(sorted(fixed_args))  # type: ignore[attr-defined]
    return fixed_args_tool


def _resolve_prompt_protocol_symbols(raw_symbols: dict[str, str], config_path: Path) -> dict[str, str]:
    resolved_symbols: dict[str, str] = {}
    resolving_symbols: set[str] = set()

    def _resolve(name: str) -> str:
        if name in resolved_symbols:
            return resolved_symbols[name]
        if name in resolving_symbols:
            raise ValueError(f"Cyclic prompt protocol variable reference detected for '{name}' in {config_path}.")
        if name not in raw_symbols:
            raise ValueError(f"Prompt protocol references undefined variable '{name}' in {config_path}.")

        resolving_symbols.add(name)
        raw_value = raw_symbols[name]

        def _replace(match: re.Match[str]) -> str:
            ref_name = match.group(1)
            if ref_name not in raw_symbols:
                raise ValueError(f"Prompt protocol references undefined variable '{ref_name}' in {config_path}.")
            return _resolve(ref_name)

        try:
            resolved_value = _PROMPT_PROTOCOL_VAR_PATTERN.sub(_replace, raw_value)
        finally:
            resolving_symbols.remove(name)

        resolved_symbols[name] = resolved_value
        return resolved_value

    for symbol_name in raw_symbols:
        _resolve(symbol_name)
    return resolved_symbols


def _expand_prompt_protocol_string(value: str, symbols: dict[str, str], config_path: Path) -> str:
    def _replace(match: re.Match[str]) -> str:
        symbol_name = match.group(1)
        if symbol_name not in symbols:
            raise ValueError(f"Prompt protocol references undefined variable '{symbol_name}' in {config_path}.")
        return symbols[symbol_name]

    return _PROMPT_PROTOCOL_VAR_PATTERN.sub(_replace, value)


def _load_prompt_protocol_config(path: Optional[Path] = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else _PROMPT_PROTOCOL_PATH
    if not config_path.exists():
        raise RuntimeError(f"Prompt protocol config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp)
    except Exception as exc:
        raise RuntimeError(f"Failed to load prompt protocol config from {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Prompt protocol config root must be a mapping in {config_path}.")

    prompt_protocol = raw.get("prompt_protocol")
    if not isinstance(prompt_protocol, dict):
        raise ValueError(f"Prompt protocol config requires a 'prompt_protocol' mapping in {config_path}.")
    prompt_protocol = dict(prompt_protocol)

    missing_keys = [key for key in _PROMPT_PROTOCOL_REQUIRED_KEYS if key not in prompt_protocol]
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise ValueError(f"Prompt protocol config missing required fields in {config_path}: {missing}")

    for key in _PROMPT_PROTOCOL_REQUIRED_STRING_KEYS:
        value = prompt_protocol[key]
        if not isinstance(value, str):
            raise ValueError(f"Prompt protocol field '{key}' must be a string in {config_path}.")

    output_rule_lines = prompt_protocol["output_rule_lines"]
    if not isinstance(output_rule_lines, list) or any(not isinstance(item, str) for item in output_rule_lines):
        raise ValueError(f"Prompt protocol field 'output_rule_lines' must be list[str] in {config_path}.")

    variables = prompt_protocol.get("variables", {})
    if variables is None:
        variables = {}
    if not isinstance(variables, dict):
        raise ValueError(f"Prompt protocol field 'variables' must be a mapping in {config_path}.")
    if any(not isinstance(name, str) or not name for name in variables):
        raise ValueError(f"Prompt protocol field 'variables' must use non-empty string keys in {config_path}.")
    if any(not isinstance(value, str) for value in variables.values()):
        raise ValueError(f"Prompt protocol field 'variables' must map to string values in {config_path}.")

    reserved_collisions = set(variables) & set(_PROMPT_PROTOCOL_REQUIRED_STRING_KEYS)
    if reserved_collisions:
        collided = ", ".join(sorted(reserved_collisions))
        raise ValueError(
            f"Prompt protocol variables cannot reuse reserved prompt field names in {config_path}: {collided}"
        )

    raw_symbols: dict[str, str] = {**variables}
    for key in _PROMPT_PROTOCOL_REQUIRED_STRING_KEYS:
        raw_symbols[key] = prompt_protocol[key]
    resolved_symbols = _resolve_prompt_protocol_symbols(raw_symbols, config_path)

    resolved_prompt_protocol = dict(prompt_protocol)
    for key in _PROMPT_PROTOCOL_REQUIRED_STRING_KEYS:
        resolved_prompt_protocol[key] = resolved_symbols[key]
    resolved_prompt_protocol["output_rule_lines"] = [
        _expand_prompt_protocol_string(line, resolved_symbols, config_path) for line in output_rule_lines
    ]
    if variables:
        resolved_prompt_protocol["variables"] = {
            key: resolved_symbols[key] for key in variables
        }

    return resolved_prompt_protocol


_PROMPT_PROTOCOL = _load_prompt_protocol_config()

TASK_SPEC_SECTION_HEADER = _PROMPT_PROTOCOL["task_spec_section_header"]
TASK_SPEC_SECTION_GUIDANCE_BASE = _PROMPT_PROTOCOL["task_spec_section_guidance_base"]
TASK_SPEC_WORKFLOW_GUIDANCE = _PROMPT_PROTOCOL["task_spec_workflow_guidance"]
TASK_SPEC_SECTION_GUIDANCE_TAIL = _PROMPT_PROTOCOL["task_spec_section_guidance_tail"]
TASK_SPEC_BLOCK_TEMPLATE = _PROMPT_PROTOCOL["task_spec_block_template"]
WORKFLOW_BLOCK_TEMPLATE = _PROMPT_PROTOCOL["workflow_block_template"]
TASK_REQUEST_SECTION_HEADER = _PROMPT_PROTOCOL["task_request_section_header"]
TASK_REQUEST_SECTION_GUIDANCE = _PROMPT_PROTOCOL["task_request_section_guidance"]
TASK_REQUEST_BLOCK_TEMPLATE = _PROMPT_PROTOCOL["task_request_block_template"]
TASK_SPEC_WARNING_HEADER = _PROMPT_PROTOCOL["task_spec_warning_header"]
INPUTS_SECTION_HEADER = _PROMPT_PROTOCOL["inputs_section_header"]
INPUTS_SECTION_GUIDANCE = _PROMPT_PROTOCOL["inputs_section_guidance"]
INPUTS_LIST_INTRO_LINE = _PROMPT_PROTOCOL["inputs_list_intro_line"]
INPUTS_EMPTY_LINE = _PROMPT_PROTOCOL["inputs_empty_line"]
INPUTS_BLOCK_TEMPLATE = _PROMPT_PROTOCOL["inputs_block_template"]
OUTPUT_SECTION_HEADER = _PROMPT_PROTOCOL["output_section_header"]
OUTPUT_SECTION_GUIDANCE = _PROMPT_PROTOCOL["output_section_guidance"]
OUTPUT_RULE_HEADER = _PROMPT_PROTOCOL["output_rule_header"]
OUTPUT_RULE_LINES = tuple(_PROMPT_PROTOCOL["output_rule_lines"])
OUTPUT_BLOCK_TEMPLATE = _PROMPT_PROTOCOL["output_block_template"]
FINAL_BRIDGE_INSTRUCTION = _PROMPT_PROTOCOL["final_bridge_instruction"]
SUPERVISOR_BRIDGE_INSTRUCTION = _PROMPT_PROTOCOL["supervisor_bridge_instruction"]
WORKFLOW_EXECUTION_INTRO = _PROMPT_PROTOCOL["workflow_execution_intro"]
WORKFLOW_OUTER_INDENT = _PROMPT_PROTOCOL["workflow_outer_indent"]
WORKFLOW_INNER_INDENT = _PROMPT_PROTOCOL["workflow_inner_indent"]

MERMAID_BLOCK_PATTERN = re.compile(r"```mermaid\s*\n(.*?)\n\s*```", flags=re.DOTALL | re.IGNORECASE)
_TASK_SPEC_RENDER_CACHE: dict[str, tuple[str, tuple[str, ...], bool]] = {}
_MERMAID_VALIDATION_CACHE: dict[str, Optional[str]] = {}
_MERMAID_VALIDATOR = None
_MERMAID_VALIDATOR_IMPORT_ERROR: Optional[str] = None


def _get_mermaid_validator():
    """Resolve mermaid syntax validator lazily to avoid hard import failures."""
    global _MERMAID_VALIDATOR, _MERMAID_VALIDATOR_IMPORT_ERROR
    if _MERMAID_VALIDATOR is not None:
        return _MERMAID_VALIDATOR
    if _MERMAID_VALIDATOR_IMPORT_ERROR is not None:
        return None
    try:
        # Third-party package: mermaid-syntax-parser
        from mermaid_parser import validate_mermaid
        _MERMAID_VALIDATOR = validate_mermaid
    except Exception as exc:
        _MERMAID_VALIDATOR_IMPORT_ERROR = str(exc)
        return None
    return _MERMAID_VALIDATOR


def _validate_mermaid_text(mermaid_text: str) -> Optional[str]:
    """
    Validate Mermaid content and return warning text when invalid.

    Returns:
        Optional[str]: warning message when invalid/unavailable, otherwise None.
    """
    validator = _get_mermaid_validator()
    if validator is None:
        details = _MERMAID_VALIDATOR_IMPORT_ERROR or "validator unavailable"
        return f"Mermaid validation skipped: {details}"

    try:
        is_valid = bool(validator(mermaid_text))
    except Exception as exc:
        return f"Mermaid validation failed with exception: {exc}"

    if not is_valid:
        return "Mermaid syntax validation failed for workflow block #1."
    return None


def _render_indented_workflow_block(mermaid_text: str) -> str:
    """Render workflow block with stable visual indentation."""
    workflow_lines = [f"{WORKFLOW_OUTER_INDENT}<workflow>"]
    for line in mermaid_text.splitlines():
        workflow_lines.append(f"{WORKFLOW_INNER_INDENT}{line}" if line else "")
    workflow_lines.append(f"{WORKFLOW_OUTER_INDENT}</workflow>")
    return "\n".join(workflow_lines)


def _build_task_spec_guidance(has_workflow: bool) -> str:
    guidance_parts = [TASK_SPEC_SECTION_GUIDANCE_BASE]
    if has_workflow:
        guidance_parts.append(TASK_SPEC_WORKFLOW_GUIDANCE)
    guidance_parts.append(TASK_SPEC_SECTION_GUIDANCE_TAIL)
    return " ".join(guidance_parts)


def _render_task_spec_content(task_spec_source: str) -> tuple[str, list[str], bool]:
    """
    Render task spec content:
    - keep non-mermaid text as task context
    - wrap mermaid blocks with <workflow>
    - append validation warnings for invalid mermaid
    """
    normalized_source = (task_spec_source or "").strip()
    if not normalized_source:
        normalized_source = "No task specification was provided."

    source_hash = hashlib.sha256(normalized_source.encode("utf-8")).hexdigest()
    cached = _TASK_SPEC_RENDER_CACHE.get(source_hash)
    if cached is not None:
        rendered_content, warning_tuple, has_workflow = cached
        return rendered_content, list(warning_tuple), has_workflow

    rendered_parts: list[str] = []
    warnings: list[str] = []
    has_workflow = False
    block_index = 0
    cursor = 0

    matches = list(MERMAID_BLOCK_PATTERN.finditer(normalized_source))
    if not matches:
        _TASK_SPEC_RENDER_CACHE[source_hash] = (normalized_source, tuple(), False)
        return normalized_source, [], False

    for match in matches:
        text_prefix = normalized_source[cursor:match.start()].strip()
        if text_prefix:
            rendered_parts.append(text_prefix)

        block_index += 1
        mermaid_text = match.group(1).strip()
        if mermaid_text:
            has_workflow = True
            indented_workflow_block = _render_indented_workflow_block(mermaid_text)
            rendered_parts.append(f"{WORKFLOW_EXECUTION_INTRO}\n{indented_workflow_block}")
            block_hash = hashlib.sha256(mermaid_text.encode("utf-8")).hexdigest()
            warning = _MERMAID_VALIDATION_CACHE.get(block_hash)
            if warning is None and block_hash not in _MERMAID_VALIDATION_CACHE:
                warning = _validate_mermaid_text(mermaid_text)
                _MERMAID_VALIDATION_CACHE[block_hash] = warning
            if warning:
                if "block #1" in warning:
                    warning = warning.replace("block #1", f"block #{block_index}")
                warnings.append(warning)

        cursor = match.end()

    text_suffix = normalized_source[cursor:].strip()
    if text_suffix:
        rendered_parts.append(text_suffix)

    rendered_content = "\n\n".join(part for part in rendered_parts if part).strip()
    if not rendered_content:
        rendered_content = normalized_source

    _TASK_SPEC_RENDER_CACHE[source_hash] = (rendered_content, tuple(warnings), has_workflow)
    return rendered_content, warnings, has_workflow


def _build_task_spec_block(task_spec_source: str, logger: Optional[AgentLogger] = None) -> tuple[str, bool]:
    rendered_content, warnings, has_workflow = _render_task_spec_content(task_spec_source)
    log = get_logger(logger, __name__)
    if warnings:
        for warning in warnings:
            log.warning(f"[YamlAgentFactory] {warning}")
        warning_lines = [TASK_SPEC_WARNING_HEADER]
        warning_lines.extend([f"{idx}. {msg}" for idx, msg in enumerate(warnings, start=1)])
        rendered_content = f"{rendered_content}\n\n" + "\n".join(warning_lines)
    return TASK_SPEC_BLOCK_TEMPLATE.format(content=rendered_content), has_workflow


def _workflow_to_task_spec_source(workflow: Any) -> str:
    """Render validated workflow text without adding list-stage labels."""
    return "\n\n".join(AgentConfigNormalizer.normalize_workflow_items(workflow))


class YamlConfiguredAgent(RoleDrivenAgent):
    """
    Dynamic agent based on YAML configuration.

    Define agent attributes such as name, description, and tools in YAML
    to enable fast configuration and deployment.
    """

    REQUIRED_CONFIG_FIELDS = RoleDrivenAgent.COMMON_REQUIRED_FIELDS

    def _build_normalized_config(self) -> NormalizedAgentConfig:
        return AgentConfigNormalizer.build_worker_normalized_config(
            self._config,
            agent_root=AGENT_ROOT,
            source_name="agent",
        )

    def _execution_validation_agent_root(self) -> str:
        return str(AGENT_ROOT)

    def process_tool_query(self, query):
        return query

    def _role_profile(self) -> AgentRoleProfile:
        effective = getattr(self, "_effective_agent_config", None)
        code_agent_cfg = get_code_agent_config(effective)
        return AgentRoleProfile(
            agent_type=AgentType.WORKER,
            tool_call_type=self._resolve_tool_call_type(),
            cache_runtime_agent=False,
            enable_sub_task_tracking=True,
            additional_authorized_imports=code_agent_cfg.get('additional_authorized_imports', []),
            inject_default_file_tools=False
        )

    def _runtime_agent_name(self) -> Optional[str]:
        return self.name

    def _runtime_agent_description(self) -> Optional[str]:
        return self.description

    def _get_tools(self):
        """Get the tool list from configuration."""
        logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        effective = getattr(self, "_effective_agent_config", None)
        tools, mcp_manager = YamlAgentFactory.get_tools_from_config(self._config, logger=logger, effective_agent_config=effective)
        self._mcp_manager = mcp_manager
        return tools

    def agent_as_tool(self):
        """
        Dynamically generate an agent tool from YAML configuration.

        **Factory mode (thread-safe)**: Each call to the returned tool function
        creates a *new* Agent instance (sharing Model, Config, Logger, Tools)
        so that concurrent calls don't corrupt shared ``memory.steps`` / ``state``.

        The returned tool also has a ``.batch()`` method for parallel execution:
        ``tool.batch(tasks)`` automatically reads the YAML ``concurrency`` field
        and uses ``ParallelAgentExecutor`` under the hood.
        """
        normalized = self._ensure_normalized()
        schema = normalized.agent_function_schema
        if schema is None:
            return None

        # Tool metadata is defined by worker top-level fields and schema.
        function_name = self.name
        inputs_schema: dict[str, dict[str, Any]] = schema["inputs"]
        required_names = [name for name, spec in inputs_schema.items() if spec.get("required", True)]
        optional_names = [name for name, spec in inputs_schema.items() if not spec.get("required", True)]
        ordered_input_names = required_names + optional_names

        # ── Factory mode: capture shared immutable state ──
        # Agent instances are stateful (memory.steps, state, step_number),
        # so we create a NEW agent per call. These components are safe to share:
        _shared_model = getattr(self, "_model", None) or getattr(self, "model", None)
        _shared_logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        _shared_execution_env = getattr(self, "_execution_env", None)
        _frozen_config = self._config  # read-only dict
        _AgentClass = self.__class__
        _self_ref = self
        _yaml_concurrency = self._config.get("concurrency")  # "auto" / int / None
        _model_type = self._config.get("model_type", "powerful")

        # A missing model means the caller is using an already-constructed test/mock agent.
        # Reusing that instance keeps the call on the patched run() implementation.
        _factory_mode = _shared_model is not None

        def _create_fresh_agent():
            """Create a new Agent instance for thread-safe execution."""
            if not _factory_mode:
                return _self_ref
            return _AgentClass(
                config=_frozen_config,
                model=_shared_model,
                execution_env=_shared_execution_env,
                logger=_shared_logger,
            )

        def _build_formatted_query(input_payload):
            """Build the formatted query string from input payload."""
            prompt_inputs = {k: v for k, v in input_payload.items() if v is not None}
            input_lines = [INPUTS_LIST_INTRO_LINE]
            item_index = 1
            for name in ordered_input_names:
                if name not in prompt_inputs:
                    continue
                description = inputs_schema[name]["description"]
                value = prompt_inputs[name]
                if isinstance(value, (dict, list)):
                    value_text = json.dumps(value, ensure_ascii=False, default=str)
                else:
                    value_text = str(value)
                input_lines.append(f"{item_index}. {description}: {value_text}")
                item_index += 1
            if item_index == 1:
                input_lines.append(INPUTS_EMPTY_LINE)
            inputs_block = INPUTS_BLOCK_TEMPLATE.format(content="\n".join(input_lines))
            # Use a fresh agent's process_tool_query (stateless method)
            query = inputs_block
            output_lines = [
                schema["output"]["description"],
                "",
                OUTPUT_RULE_HEADER,
                *OUTPUT_RULE_LINES,
            ]
            output_block = OUTPUT_BLOCK_TEMPLATE.format(content="\n".join(output_lines))

            workflow = _workflow_to_task_spec_source(_frozen_config['workflow'])
            task_spec_block, has_workflow = _build_task_spec_block(workflow, logger=_shared_logger)
            task_spec_guidance = _build_task_spec_guidance(has_workflow)
            formatted_query = (
                f"{TASK_SPEC_SECTION_HEADER}\n"
                f"{task_spec_guidance}\n"
                f"{task_spec_block}\n\n"
                f"{INPUTS_SECTION_HEADER}\n"
                f"{INPUTS_SECTION_GUIDANCE}\n"
                f"{query}\n\n"
                f"{OUTPUT_SECTION_HEADER}\n"
                f"{OUTPUT_SECTION_GUIDANCE}\n"
                f"{output_block}\n\n"
                f"{FINAL_BRIDGE_INSTRUCTION}"
            )
            return formatted_query

        # Dynamically create the tool function (factory mode)
        def dynamic_agent_tool(*args, **kwargs) -> str:
            if len(args) > len(ordered_input_names):
                raise TypeError(
                    f"{function_name}() takes {len(ordered_input_names)} positional arguments but {len(args)} were given"
                )

            input_payload: dict[str, Any] = {}
            for idx, value in enumerate(args):
                input_payload[ordered_input_names[idx]] = value

            for key, value in kwargs.items():
                if key not in inputs_schema:
                    raise TypeError(f"{function_name}() got an unexpected keyword argument '{key}'")
                if key in input_payload:
                    raise TypeError(f"{function_name}() got multiple values for argument '{key}'")
                input_payload[key] = value

            missing_required = [name for name in required_names if name not in input_payload]
            if missing_required:
                raise TypeError(
                    f"{function_name}() missing {len(missing_required)} required positional argument(s): "
                    + ", ".join(missing_required)
                )

            for name in optional_names:
                input_payload.setdefault(name, None)

            state_args = {k: v for k, v in input_payload.items() if v is not None}
            formatted_query = _build_formatted_query(input_payload)

            # Factory mode: create a NEW agent for each call (thread-safe)
            agent = _create_fresh_agent()
            result = agent.run(formatted_query, additional_args=state_args)
            # NOTE: _current_worker_memory is now set INSIDE _execute_agent()
            # (P2 fix — the old SET here was too late for GET in _execute_with_lifecycle)

            result_str = "" if result is None else str(result)
            from src.lib.context_engine.runtime import get_active_context_engine

            engine = get_active_context_engine()
            if engine is not None:
                return (
                    engine.compress_tool_result(
                        result_str,
                        tool_name=function_name,
                        source=f"worker_result:{function_name}",
                    )
                    or result_str
                )
            return result_str

        # ── Attach .batch() method for parallel execution ──
        def batch(tasks, concurrency=None, on_progress=None):
            """
            Execute multiple tasks in parallel using this agent tool.

            Concurrency priority: ``concurrency`` param > YAML ``concurrency`` field > auto.

            Args:
                tasks: List of dicts, each passed as ``**kwargs`` to this tool.
                concurrency: Override concurrency (int or ``"auto"``). If None,
                             reads from YAML config; if YAML also unset, uses auto.
                on_progress: Optional callback ``(completed, total, TaskResult)``.

            Returns:
                List[TaskResult]: One result per task.
            """
            from src.lib.concurrency import ParallelAgentExecutor

            # Priority chain: param > YAML > None (auto)
            effective = concurrency if concurrency is not None else _yaml_concurrency
            # Normalize "auto" string to None (executor treats None as auto)
            if effective == "auto":
                effective = None

            executor = ParallelAgentExecutor(
                max_workers=effective,
                model_type=_model_type,
            )
            return executor.execute_batch(tasks, dynamic_agent_tool, on_progress)

        dynamic_agent_tool.batch = batch
        # Store metadata for introspection
        dynamic_agent_tool._agent_loom_concurrency = _yaml_concurrency
        dynamic_agent_tool._agent_loom_model_type = _model_type

        description_lines = [schema["description"], "", "Args:"]
        for name in ordered_input_names:
            spec = inputs_schema[name]
            required_tag = "required" if spec.get("required", True) else "optional"
            description_lines.append(
                f"    {name} ({spec['type']}, {required_tag}): {spec['description']}"
            )
        description_lines.extend(
            [
                "",
                "Returns:",
                f"    str: {schema['output']['description']}",
            ]
        )
        generated_docstring = "\n".join(description_lines)

        # Set dynamic function name and docstring
        dynamic_agent_tool.__name__ = function_name
        dynamic_agent_tool.__doc__ = generated_docstring

        annotations: dict[str, Any] = {"return": str}
        signature_params = []
        for name in ordered_input_names:
            param_spec = inputs_schema[name]
            annotations[name] = str
            default = inspect.Parameter.empty if param_spec.get("required", True) else None
            signature_params.append(
                inspect.Parameter(
                    name=name,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=str,
                )
            )

        dynamic_agent_tool.__annotations__ = annotations
        dynamic_agent_tool.__signature__ = inspect.Signature(
            parameters=signature_params,
            return_annotation=str,
        )

        # Fail fast: ensure the generated function can be converted into tool schema.
        try:
            from smolagents.tools import get_json_schema

            get_json_schema(dynamic_agent_tool)
        except Exception as e:
            raise ValueError(
                f"Failed to generate agent tool schema for '{function_name}': {e}"
            ) from e

        return dynamic_agent_tool


class YamlConfiguredSupervisorAgent(RoleDrivenAgent):
    """
    Supervisor agent based on YAML configuration.
    """
    REQUIRED_CONFIG_FIELDS = RoleDrivenAgent.COMMON_REQUIRED_FIELDS

    def _before_config_validation(self, **kwargs) -> None:
        # Extract yaml_file_path from config
        yaml_file_path = self._config.get('_yaml_file_path')
        self._yaml_file_path = Path(yaml_file_path) if yaml_file_path else None

        # Infer category from file path
        if self._yaml_file_path:
            self._inferred_category = infer_category_from_yaml_path(self._yaml_file_path)
            return
        raise ValueError("Configuration file path not found: cannot get _yaml_file_path from config")

    def _build_normalized_config(self) -> NormalizedAgentConfig:
        return AgentConfigNormalizer.build_supervisor_normalized_config(
            self._config,
            agent_root=AGENT_ROOT,
            source_name="supervisor",
        )

    def _execution_validation_agent_root(self) -> str:
        return str(AGENT_ROOT)

    def _validate_role_specific_config(self, normalized: Any | None) -> None:
        AgentConfigNormalizer.validate_worker_agents_config(self._config.get('worker_agents', []))

    @property
    def workflow_category(self) -> str:
        """Return the inferred category."""
        return self._inferred_category

    def _role_profile(self) -> AgentRoleProfile:
        execution_normalized = self._ensure_execution_normalized()
        execution_env_type = execution_normalized.executor_type
        inject_default_file_tools = execution_env_type not in {"docker", "e2b"}
        # If user explicitly set default_loaded_tools: [] (empty list) in YAML,
        # suppress auto-injected file tools to respect the "no default tools" intent.
        cfg_default_tools = self._config.get("default_loaded_tools")
        if isinstance(cfg_default_tools, list) and len(cfg_default_tools) == 0:
            inject_default_file_tools = False
        # Also honour an explicit inject_default_file_tools: false in the YAML.
        if "inject_default_file_tools" in self._config:
            inject_default_file_tools = bool(self._config["inject_default_file_tools"])
        return AgentRoleProfile(
            agent_type=AgentType.SUPERVISOR,
            tool_call_type=self._resolve_tool_call_type(),
            cache_runtime_agent=True,
            enable_sub_task_tracking=False,
            additional_authorized_imports=['*'],
            inject_default_file_tools=inject_default_file_tools
        )

    def _transform_task(self, task: str) -> str:
        workflow_content = _workflow_to_task_spec_source(self._config['workflow'])
        description = self._config.get('description', '').strip()
        task_spec_source = workflow_content.strip()
        if description:
            task_spec_source = f"{description}\n\n{task_spec_source}" if task_spec_source else description

        logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        task_spec_block, has_workflow = _build_task_spec_block(task_spec_source, logger=logger)
        task_spec_guidance = _build_task_spec_guidance(has_workflow)
        task_request_block = TASK_REQUEST_BLOCK_TEMPLATE.format(content=task)

        enhanced_task = (
            f"{TASK_SPEC_SECTION_HEADER}\n"
            f"{task_spec_guidance}\n"
            f"{task_spec_block}\n\n"
            f"{TASK_REQUEST_SECTION_HEADER}\n"
            f"{TASK_REQUEST_SECTION_GUIDANCE}\n"
            f"{task_request_block}\n\n"
            f"{SUPERVISOR_BRIDGE_INSTRUCTION}"
        )
        return enhanced_task

    def _transform_tasks(self, task: str) -> list[str]:
        workflow_content = self._config['workflow']
        if isinstance(workflow_content, list):
            # List workflows are executed sequentially: each item becomes a
            # separate runtime_agent.run() call with reset=False preserving
            # memory from previous steps.  The original task data (e.g. sample
            # row content) is injected into the first item via <inputs> block
            # so it is visible to the agent in step 1; subsequent steps access
            # it through preserved memory.steps.
            items = list(AgentConfigNormalizer.normalize_workflow_items(workflow_content))
            if task.strip():
                inputs_block = INPUTS_BLOCK_TEMPLATE.format(content=task)
                items[0] = f"{items[0]}\n\n{inputs_block}"
            return items
        return [self._transform_task(task)]

    def _get_tools(self) -> List:
        """Get the tool list from configuration."""
        tools = []
        worker_logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        log = get_logger(worker_logger, __name__)

        # Load standard tools (including tools from skills when allowed_tools is configured)
        effective = getattr(self, "_effective_agent_config", None)
        standard_tools, mcp_manager = YamlAgentFactory.get_tools_from_config(self._config, logger=worker_logger, effective_agent_config=effective)
        self._mcp_manager = mcp_manager
        tools.extend(standard_tools)

        worker_agents_folder = get_worker_agent_yaml_path(self.workflow_category)
        expected_agents = self._config.get('worker_agents', [])

        resolved_worker_agents = AgentConfigNormalizer.precheck_worker_agent_paths(
            expected_agents,
            worker_agents_folder,
            agent_root=AGENT_ROOT,
        )

        for configured_path, found_file in resolved_worker_agents:
            try:
                # Load configuration
                agent_config = YamlAgentFactory._load_config_from_file(found_file)

                # Create agent tool
                agent_tool = YamlAgentFactory.create_agent_as_tool(
                    agent_config,
                    execution_env=self._execution_env,
                    logger=worker_logger
                )
                if agent_tool is not None:
                    tools.append(agent_tool)
            except Exception as e:
                msg = f"Failed to load worker agent from path '{configured_path}' ({found_file}): {e}"
                log.error(msg)
                raise ValueError(msg) from e

        log.info("[YamlConfiguredSupervisorAgent] Successfully loaded all tools for supervisor agent.")
        return tools


def _load_mcp_tools(
    *,
    config: dict,
    effective_agent_config: Optional[dict],
    agent_root: Path,
    append_tool: Callable,
    log: Any,
) -> Optional[Any]:
    """Load MCP tools from ``mcp_servers`` config and append them via *append_tool*.

    Returns a :class:`McpManager` instance when at least one MCP server is
    configured, or ``None`` otherwise.  Failures are logged — never raised.
    """
    global_raw = (effective_agent_config or {}).get("mcp_servers")
    agent_raw = config.get("mcp_servers")

    if global_raw is None and agent_raw is None:
        return None

    try:
        from src.mcp.config import parse_mcp_yaml_value, merge_mcp_configs
        from src.mcp.manager import McpManager

        global_settings = parse_mcp_yaml_value(global_raw, agent_root) if global_raw is not None else None
        agent_settings = parse_mcp_yaml_value(agent_raw, agent_root) if agent_raw is not None else None
        merged = merge_mcp_configs(global_settings, agent_settings)

        if merged is None or not merged.configs:
            log.debug("[MCP] No MCP servers configured after parsing")
            return None

        manager = McpManager(merged)
        manager.connect_all()
        for tool in manager.get_all_tools():
            append_tool(tool)
        return manager

    except ImportError as exc:
        log.warning("[MCP] MCP support not available: %s", exc)
        return None
    except Exception as exc:
        log.warning("[MCP] Unexpected error loading MCP tools: %s", exc)
        return None


class YamlAgentFactory:
    """
    YAML agent factory class.

    Provides capabilities for creating agent tools from YAML configuration.
    Supports loading from .yaml files and .md files that contain YAML code blocks.
    """

    @staticmethod
    def get_tools_from_config(config: dict, logger: Optional[AgentLogger] = None, effective_agent_config: Optional[dict] = None) -> tuple:
        """Load tool list from a configuration dictionary.

        Returns
        -------
        tuple[list, McpManager | None]
            A 2-tuple of (tools, mcp_manager).  ``mcp_manager`` is ``None``
            when no MCP servers are configured.
        """
        log = get_logger(logger, __name__)
        tools = []
        seen = set()

        def _tool_name(tool_obj) -> Optional[str]:
            return getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", None)

        def _append_tool(tool_obj, explicit_name: Optional[str] = None):
            tool_name = explicit_name or _tool_name(tool_obj)
            if tool_name and tool_name in seen:
                return
            if tool_name:
                seen.add(tool_name)
            tools.append(tool_obj)

        env_cfg = config.get("execution_env", {})
        execution_env_type = "local"
        if isinstance(env_cfg, dict):
            raw_type = env_cfg.get("type")
            if isinstance(raw_type, str) and raw_type.strip():
                execution_env_type = raw_type.strip().lower()

        # Remote executors validate tool bodies more strictly and many local filesystem
        # helpers are intentionally unavailable there, so default tools are skipped.
        if execution_env_type in {"docker", "e2b"}:
            log.info(
                "[YamlAgentFactory] Skip loading default tools for execution_env.type='%s'",
                execution_env_type,
            )
        else:
            # Agent-level config takes priority: if the agent YAML explicitly declares
            # `default_loaded_tools`, use that instead of the global effective config.
            if "default_loaded_tools" in config:
                default_tools_source = config
            else:
                default_tools_source = effective_agent_config
            # Load default tools from unified config (config/system.yaml + overrides)
            for tool_name in get_default_tools(default_tools_source):
                try:
                    tool_function = resolve_tool_function(tool_name)
                    _append_tool(tool_function, explicit_name=tool_name)
                    log.info(f"[YamlAgentFactory] Loaded default tool: {tool_name}")
                except ValueError:
                    log.warning(f"[YamlAgentFactory] Default tool not found: {tool_name}")

        if 'tools' not in config:
            # Still check for MCP tools even when no explicit tools are listed.
            mcp_manager = _load_mcp_tools(
                config=config,
                effective_agent_config=effective_agent_config,
                agent_root=AGENT_ROOT,
                append_tool=_append_tool,
                log=log,
            )
            return tools, mcp_manager

        raw_tools = config['tools']
        AgentConfigNormalizer.validate_tools_config_entries(raw_tools)

        for tool_config in raw_tools:
            tool_name = tool_config.get('name')
            fixed_args = _get_fixed_tool_args(tool_config)

            # Check whether this is a dynamically loaded tool
            if 'module' in tool_config and 'function' in tool_config:
                # Use dynamic loading
                module = tool_config['module']
                function = tool_config['function']
                try:
                    loaded_function = load_function(module, function)
                except (ImportError, AttributeError, TypeError) as e:
                    log.error(f"[YamlAgentFactory] Failed to load dynamic tool: {tool_name} from {module}.{function}. Error: {e}")
                    raise ValueError(f"Failed to dynamically load tool '{tool_name}': {e}") from e

                loaded_function = _bind_fixed_tool_args(loaded_function, tool_name, fixed_args)
                _append_tool(loaded_function, explicit_name=tool_name)
                log.info(f"[YamlAgentFactory] Successfully loaded dynamic tool: {tool_name} from {module}.{function}")
            else:
                # Convention-based resolution via src.tools attributes
                try:
                    tool_function = resolve_tool_function(tool_name)
                except ValueError as e:
                    log.error(f"[YamlAgentFactory] Failed to find predefined tool: {tool_name}")
                    raise ValueError(f"Tool '{tool_name}' not found, please verify the tool name") from e
                tool_function = _bind_fixed_tool_args(tool_function, tool_name, fixed_args)
                _append_tool(tool_function)
                log.info(f"[YamlAgentFactory] Successfully loaded predefined tool: {tool_name}")

        # Phase 3: MCP tools (connect to external MCP servers)
        mcp_manager = _load_mcp_tools(
            config=config,
            effective_agent_config=effective_agent_config,
            agent_root=AGENT_ROOT,
            append_tool=_append_tool,
            log=log,
        )

        # Phase 4: Patch shell_tool description with dynamic security policy
        from src.lib.permissions.policy_summary import patch_shell_tool_security
        patch_shell_tool_security(tools, log)

        return tools, mcp_manager

    @staticmethod
    def _extract_yaml_from_markdown(content: str) -> tuple[dict, str]:
        """
        Extract YAML configuration and workflow content from Markdown.

        Args:
            content: Markdown file content.

        Returns:
            tuple: (yaml_config, workflow_content)
        """
        # Find YAML code block
        yaml_pattern = r'```yaml\s*\n(.*?)\n```'
        match = re.search(yaml_pattern, content, re.DOTALL)

        if not match:
            raise ValueError("No YAML code block found in markdown file")

        yaml_content = match.group(1)
        yaml_config = yaml.safe_load(yaml_content)

        # Remove YAML code block; remaining content is workflow
        workflow_content = re.sub(yaml_pattern, '', content, flags=re.DOTALL).strip()

        # Add workflow content into configuration
        if workflow_content:
            yaml_config['workflow'] = workflow_content

        return yaml_config, workflow_content

    @staticmethod
    def _prepare_agent_config(config: dict, *, source_path: Union[str, Path, None] = None) -> dict:
        if not isinstance(config, dict):
            raise ValueError(f"Agent configuration must be a mapping, got {type(config).__name__}")

        prepared = copy.deepcopy(config)
        if source_path is not None:
            prepared["_yaml_file_path"] = str(Path(source_path).resolve())
            return prepared

        raw_path = prepared.get("_yaml_file_path")
        if isinstance(raw_path, str) and raw_path.strip():
            prepared["_yaml_file_path"] = str(Path(raw_path).expanduser().resolve())
        return prepared

    @staticmethod
    def _load_config_from_file(config_path: Union[str, Path]) -> dict:
        """
        Load configuration from file, supporting .yaml and .md files.

        Args:
            config_path: Configuration file path.

        Returns:
            dict: Parsed configuration dictionary.
        """
        config_path = Path(config_path)

        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if config_path.suffix.lower() == '.md':
            config, _ = YamlAgentFactory._extract_yaml_from_markdown(content)
        elif config_path.suffix.lower() in ['.yaml', '.yml']:
            config = yaml.safe_load(content)
        else:
            raise ValueError(f"Unsupported file format: {config_path.suffix}")
        
        return YamlAgentFactory._prepare_agent_config(config, source_path=config_path)

    @staticmethod
    def create_agent_tool(config_path: Union[str, Path, dict],
                         agent_class=None,
                         model=None, execution_env=None) -> List:
        """
        Create an agent tool from YAML configuration.

        Args:
            config_path: YAML/Markdown config file path or config dictionary.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.
            model: Optional model instance.
            execution_env: Optional execution environment instance.

        Returns:
            List: List of functions decorated by @tool.
        """
        # Use custom class or default YamlConfiguredAgent
        AgentClass = agent_class or YamlConfiguredAgent

        # Use dict directly when provided; otherwise load from file
        if isinstance(config_path, dict):
            config = YamlAgentFactory._prepare_agent_config(config_path)
        else:
            config = YamlAgentFactory._load_config_from_file(config_path)

        # Create configured agent
        agent = AgentClass(
            config=config,
            model=model,
            execution_env=execution_env
        )

        # Return agent tool list directly; tools are already decorated with @tool
        return agent._get_tools()

    # ── Tool cache: keyed by resolved file path, thread-safe ──
    _tool_cache: Dict[str, Callable] = {}
    _tool_cache_lock = threading.Lock()

    @classmethod
    def clear_tool_cache(cls) -> None:
        """Clear the agent-as-tool cache. Primarily for testing."""
        with cls._tool_cache_lock:
            cls._tool_cache.clear()

    @staticmethod
    def create_agent_as_tool(config_path: Union[str, Path, dict],
                            agent_class=None,
                            model=None,
                            execution_env=None,
                            logger: Optional[AgentLogger]=None,
                            **kwargs
                            ) -> Optional[Callable]:
        """
        Create an agent-as-tool from YAML configuration.

        Returns a single callable tool function with a ``.batch()`` method
        for parallel execution, or ``None`` if the YAML has no
        ``agent_function_schema`` (meaning the agent is not exported as a tool).

        File-based configs are cached by resolved path — repeated calls
        with the same YAML file return the cached tool without re-creating
        the agent.  Dict-based configs are never cached.

        Args:
            config_path: YAML/Markdown config file path or config dictionary.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.
            model: Optional model instance.
            execution_env: Optional execution environment instance.
            logger: Optional logger instance.

        Returns:
            Optional[Callable]: The agent tool function, or None.
        """
        # ── Cache lookup (file paths only) ──
        cache_key: Optional[str] = None
        if not isinstance(config_path, dict):
            cache_key = str(Path(config_path).resolve())
            cached = YamlAgentFactory._tool_cache.get(cache_key)
            if cached is not None:
                return cached

        # Use custom class or default YamlConfiguredAgent
        AgentClass = agent_class or YamlConfiguredAgent

        # Use dict directly when provided; otherwise load from file
        if isinstance(config_path, dict):
            config = YamlAgentFactory._prepare_agent_config(config_path)
        else:
            config = YamlAgentFactory._load_config_from_file(config_path)

        # Create configured agent
        agent = AgentClass(
            config=config,
            model=model,
            execution_env=execution_env,
            logger=logger,
            **kwargs
        )

        # Return single agent tool or None
        tool = agent.agent_as_tool()
        effective_logger = logger or getattr(agent, "logger", None) or getattr(agent, "_logger", None)
        log = get_logger(effective_logger, __name__)
        if tool is not None:
            log.info(f"[YamlAgentFactory] Successfully created agent tool: {tool.__name__} from {config_path if not isinstance(config_path, dict) else 'dict'}")
            # ── Store in cache ──
            if cache_key is not None:
                with YamlAgentFactory._tool_cache_lock:
                    YamlAgentFactory._tool_cache[cache_key] = tool
        else:
            log.error(f"[YamlAgentFactory] Failed to create agent tool from {config_path if not isinstance(config_path, dict) else 'dict'} (disabled or missing config)")
        return tool


    @staticmethod
    def run_agents_parallel(
        config_path: Union[str, Path, dict],
        tasks: list,
        max_workers: Optional[int] = None,
        logger: Optional[AgentLogger] = None,
        on_progress=None,
    ) -> list:
        """
        Create an Agent-as-Tool and execute a batch of tasks in parallel.

        This is a convenience method that combines ``create_agent_as_tool()``
        with ``ParallelAgentExecutor.execute_batch()``, encapsulating the
        common pattern of creating a worker agent and calling it concurrently
        on multiple inputs (e.g. analysing many directories in parallel).

        Args:
            config_path: YAML/Markdown config file path or config dict.
            tasks: List of dicts, each passed as ``**kwargs`` to the agent tool.
            max_workers: Max parallel threads (default: auto from RPM).
            logger: Optional logger instance.
            on_progress: Optional callback ``(completed, total, TaskResult)``.

        Returns:
            List[TaskResult]: One result per task.
        """
        from src.lib.concurrency import ParallelAgentExecutor

        agent_tool = YamlAgentFactory.create_agent_as_tool(config_path, logger=logger)
        if agent_tool is None:
            raise RuntimeError(
                f"Failed to create agent tool from "
                f"{config_path if not isinstance(config_path, dict) else 'dict'}"
            )

        # Determine model_type for the executor
        if isinstance(config_path, dict):
            model_type = config_path.get("model_type", "powerful")
        else:
            try:
                config = YamlAgentFactory._load_config_from_file(config_path)
                model_type = config.get("model_type", "powerful")
            except Exception:
                model_type = "powerful"

        executor = ParallelAgentExecutor(
            max_workers=max_workers,
            model_type=model_type,
        )
        return executor.execute_batch(tasks, agent_tool, on_progress)

    @staticmethod
    def create_agents_as_tools_from_folder(folder_path: Union[str, Path],
                                          agent_class=None,
                                          model=None,
                                          execution_env=None,
                                          logger: Optional[AgentLogger]=None,
                                          **kwargs
                                          ) -> List:
        """
        Load all YAML and Markdown files from a folder and create agent-as-tools.

        Args:
            folder_path: Folder path containing YAML/Markdown config files.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.
            model: Optional model instance.
            execution_env: Optional execution environment instance.
            logger: Optional logger instance.

        Returns:
            List: List of all dynamically generated agent tool functions.
        """
        folder_path = Path(folder_path)
        all_tools = []
        log = get_logger(logger, __name__)

        if not folder_path.exists() or not folder_path.is_dir():
            return all_tools

        # Iterate through all YAML and Markdown files in the folder
        for config_file in list(folder_path.glob("*.yaml")) + list(folder_path.glob("*.yml")) + list(folder_path.glob("*.md")):
            try:
                agent_tool = YamlAgentFactory.create_agent_as_tool(
                    config_file,
                    agent_class=agent_class,
                    model=model,
                    execution_env=execution_env,
                    logger=logger,
                    **kwargs
                )
                if agent_tool is not None:
                    all_tools.append(agent_tool)
            except Exception as e:
                log.error(f"Failed to load agent from {config_file}: {e}")

        return all_tools

    @staticmethod
    def load_agents_from_directory(directory: Union[str, Path], agent_class=None) -> Dict[str, List]:
        """
        Load all YAML and Markdown config files from a directory and create agent tools.

        Args:
            directory: Directory containing YAML/Markdown configuration files.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.

        Returns:
            Dict[str, List]: Mapping from agent name to tool list.
        """
        directory = Path(directory)
        agents = {}
        log = get_logger(None, __name__)

        # Iterate through all supported configuration file formats
        for config_file in list(directory.glob("*.yaml")) + list(directory.glob("*.yml")) + list(directory.glob("*.md")):
            try:
                tools = YamlAgentFactory.create_agent_tool(config_file, agent_class=agent_class)
                # Read configuration to get the agent name
                config = YamlAgentFactory._load_config_from_file(config_file)
                agent_name = config['name']
                agents[agent_name] = tools
            except Exception as e:
                log.error(f"Failed to load config file {config_file}: {e}")

        return agents
