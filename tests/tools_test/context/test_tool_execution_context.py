"""Host Context injection through the same Gateway used by runtimes."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from threading import RLock
from unittest.mock import patch

import pytest
from agentloom.app.factory import _bind_fixed_tool_args
from agentloom.app.tool_factory import (
    bind_tool_factory_bindings,
    bind_tool_factory_context,
    bind_tool_factory_work_item,
)
from agentloom.execution.hooks import HookPlan, HookResult, HookRun
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.native_tools import NativeCallIdentity
from agentloom.execution.tool_context import (
    ToolCallContext,
    ToolContext,
    bind_tool_call_context,
    current_tool_context,
)
from agentloom.execution.tool_gateway import AgentLoomToolGateway, ToolBinding, bind_tool
from agentloom.execution.trace import ExplicitExecutionContext, bind_explicit_execution_context


@contextmanager
def execution():
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    context = ExplicitExecutionContext(
        task_id="task", sub_task_id=None, agent_id="agent", agent_name="agent",
        agent_config={}, skill_catalog=None, hook_run=run, runtime_agent_path="agent",
        root_run_id="root", local_run_id="local",
    )
    with bind_explicit_execution_context(context), patch(
        "agentloom.execution.hooks.path_validators.enforce_core_tool_guard", return_value=HookResult()
    ):
        yield


@pytest.mark.parametrize("manual_binding", [False, True])
def test_injected_context_uses_captured_dependencies_without_model_arguments(manual_binding):
    lock = RLock()  # A dependency which cannot be deep-copied or serialized.
    observed = []

    def save(text: str, *, context: ToolContext):
        assert current_tool_context() is context
        assert context.bindings["lock"] is lock
        with pytest.raises(TypeError):
            context.bindings["other"] = object()
        observed.append(context)
        return text

    with bind_tool_factory_bindings({"lock": lock}), bind_tool_factory_work_item("private-item"), \
            bind_tool_factory_context({"business": "input"}, agent_name="creator"):
        binding = bind_tool(save)
        if manual_binding:
            binding = ToolBinding(
                definition=binding.definition, inputs_schema=binding.inputs_schema, forward=save,
            )
        gateway = AgentLoomToolGateway([binding])
    assert "context" not in binding.definition.parameters["properties"]
    assert binding.definition.parameters["required"] == ["text"]
    assert "private-item" not in repr(binding.definition)
    with execution():
        prepared = gateway.prepare(call_id="provider-call", tool_name="save", arguments={"text": "value"})
        assert dict(prepared.arguments) == {"text": "value"}
        record = gateway.execute_prepared(prepared)
    assert record.status == "completed", record.reason
    assert record.input == {"text": "value"}
    assert record.output == "value"
    assert observed[0].item_id == "private-item"
    assert observed[0].agent_name == "creator"
    assert observed[0].call_id == "provider-call"
    assert observed[0].tool_name == "save"
    assert current_tool_context() is None


def test_legacy_argument_names_and_kwargs_do_not_receive_implicit_arguments():
    def legacy(ctx: str, context: str, **extra):
        assert current_tool_context().tool_name == "legacy"
        assert extra == {}
        return [ctx, context]

    gateway = AgentLoomToolGateway.from_tools([legacy])
    with execution():
        record = gateway.invoke(call_id="legacy-call", tool_name="legacy", arguments={"ctx": "one", "context": "two"})
    assert record.status == "completed", record.reason
    assert record.output == ["one", "two"]


def test_model_cannot_override_internal_context_even_with_custom_validator():
    called = []

    def save(value: str, *, trusted: ToolContext):
        called.append(trusted)
        return value

    normal = bind_tool(save)
    binding = ToolBinding(
        definition=normal.definition, inputs_schema=normal.inputs_schema, forward=save,
        input_validator=lambda _: None,
    )
    with execution():
        record = AgentLoomToolGateway([binding]).invoke(
            call_id="spoof", tool_name="save", arguments={"value": "x", "trusted": {"bindings": "forged"}},
        )
    assert record.status == "blocked"
    assert "supplied by the host" in record.reason
    assert not called


@pytest.mark.parametrize("parameters", [
    {"type": "object", "properties": {"trusted": {"type": "object"}}},
    {"anyOf": [{"type": "object", "required": ["trusted"]}]},
])
def test_explicit_schema_cannot_declare_injected_parameter(parameters):
    def save(*, trusted: ToolContext):
        return "saved"

    with pytest.raises(ValueError, match="must not appear"):
        ToolBinding(definition=ToolDefinition(name="save", description="Save.", parameters=parameters),
                    forward=save, inputs_schema={})


def test_fixed_parameter_wrapper_keeps_context_host_only():
    def label(prefix: str, text: str, *, runtime: ToolContext):
        assert runtime.tool_name == "label"
        return prefix + text

    gateway = AgentLoomToolGateway.from_tools([_bind_fixed_tool_args(label, "label", {"prefix": "fixed:"})])
    assert set(gateway.definitions[0].parameters["properties"]) == {"text"}
    with execution():
        record = gateway.invoke(call_id="fixed", tool_name="label", arguments={"text": "value"})
    assert record.status == "completed", record.reason
    assert record.output == "fixed:value"


def test_nested_failure_restores_outer_execution_context_and_native_facts():
    observed = []

    def inner(*, ctx: ToolContext):
        observed.append(ctx)
        raise ValueError("nested failure")

    inner_gateway = AgentLoomToolGateway.from_tools([inner])

    def outer(*, ctx: ToolContext):
        assert current_tool_context() is ctx
        result = inner_gateway.invoke(call_id="inner-call", tool_name="inner", arguments={})
        assert result.status == "error"
        assert current_tool_context() is ctx
        return {"native_call": ctx.identity.call_id, "batch": list(ctx.assistant_tools)}

    gateway = AgentLoomToolGateway.from_tools([outer])
    identity = NativeCallIdentity(
        application_id="application", task_id="task", run_id="run", instance_id="instance",
        native_session_id="session", native_parent_id="parent", call_id="outer-call",
    )
    with execution(), bind_tool_call_context(ToolCallContext(identity, ("outer",))):
        result = gateway.invoke(call_id="outer-call", tool_name="outer", arguments={})
    assert result.status == "completed", result.reason
    assert result.output == {"native_call": "outer-call", "batch": ["outer"]}
    assert observed[0].call_id == "inner-call"
    assert observed[0].identity is None
    assert observed[0].assistant_tools == ()
    assert current_tool_context() is None


def test_context_type_requires_one_keyword_capable_parameter():
    def two(first: ToolContext, second: ToolContext):
        pass

    def positional(ctx: ToolContext, /):
        pass

    for tool in [two, positional]:
        with pytest.raises(TypeError, match="ToolContext"):
            bind_tool(tool)


@pytest.mark.parametrize("wrapped", [False, True])
def test_future_annotations_on_callable_instances_keep_context_out_of_schema(wrapped):
    observed = []

    class TypedCallable:
        name = "typed"

        def __call__(self, value: str, *, ctx: ToolContext):
            observed.append(ctx)
            return value

    tool = TypedCallable()
    if wrapped:
        tool = partial(tool)
        tool.name = "typed"
    binding = bind_tool(tool)
    assert binding.context_parameter == "ctx"
    assert set(binding.definition.parameters["properties"]) == {"value"}
    assert binding.definition.parameters["properties"]["value"]["type"] == "string"
    with execution():
        result = AgentLoomToolGateway([binding]).invoke(
            call_id="callable-instance", tool_name="typed", arguments={"value": "saved"},
        )
    assert result.status == "completed", result.reason
    assert observed[0].call_id == "callable-instance"
    assert result.input == {"value": "saved"}


@pytest.mark.parametrize("same_name", [False, True])
def test_direct_nested_call_cannot_reuse_native_dispatch_even_with_matching_ids(same_name):
    observed = []

    def inner(*, ctx: ToolContext):
        observed.append(ctx)
        return "inner"

    inner_binding = bind_tool(inner)
    if same_name:
        inner_binding = ToolBinding(
            definition=ToolDefinition(name="outer", description="Inner.", parameters=inner_binding.definition.parameters),
            inputs_schema=inner_binding.inputs_schema, forward=inner,
        )
    inner_gateway = AgentLoomToolGateway([inner_binding])

    def outer(*, ctx: ToolContext):
        assert ctx.identity is identity
        result = inner_gateway.invoke(call_id="same", tool_name="outer" if same_name else "inner", arguments={})
        assert result.status == "completed", result.reason
        assert current_tool_context() is ctx
        return "outer"

    identity = NativeCallIdentity(
        application_id="app", task_id="task", run_id="run", instance_id="instance",
        native_session_id="session", native_parent_id="parent", call_id="same",
    )
    with execution(), bind_tool_call_context(ToolCallContext(identity, ("outer",))):
        result = AgentLoomToolGateway.from_tools([outer]).invoke(
            call_id="same", tool_name="outer", arguments={},
        )
    assert result.status == "completed", result.reason
    assert observed[0].identity is None
    assert observed[0].assistant_tools == ()


def test_nested_runtime_dispatch_binds_its_own_native_context():
    observed = []

    def inner(*, ctx: ToolContext):
        observed.append(ctx)
        return "inner"

    inner_gateway = AgentLoomToolGateway.from_tools([inner])
    outer_identity = NativeCallIdentity(
        application_id="app", task_id="task", run_id="run", instance_id="instance",
        native_session_id="session", native_parent_id="parent", call_id="same",
    )
    inner_identity = NativeCallIdentity(
        application_id="app", task_id="task", run_id="run", instance_id="instance",
        native_session_id="session", native_parent_id="nested-parent", call_id="same",
    )

    def outer(*, ctx: ToolContext):
        with bind_tool_call_context(ToolCallContext(inner_identity, ("inner",))):
            result = inner_gateway.invoke(call_id="same", tool_name="inner", arguments={})
        assert result.status == "completed", result.reason
        assert current_tool_context() is ctx
        assert ctx.identity is outer_identity
        return "outer"

    with execution(), bind_tool_call_context(ToolCallContext(outer_identity, ("outer",))):
        result = AgentLoomToolGateway.from_tools([outer]).invoke(call_id="same", tool_name="outer", arguments={})
    assert result.status == "completed", result.reason
    assert observed[0].identity is inner_identity
    assert observed[0].assistant_tools == ("inner",)


def test_native_dispatch_association_survives_separate_prepare_and_execute_scopes():
    observed = []

    def save(*, ctx: ToolContext):
        observed.append(ctx)
        return "saved"

    gateway = AgentLoomToolGateway.from_tools([save])
    identity = NativeCallIdentity(
        application_id="app", task_id="task", run_id="run", instance_id="instance",
        native_session_id="session", native_parent_id="parent", call_id="save",
    )
    with execution():
        with bind_tool_call_context(ToolCallContext(identity, ("save",))):
            prepared = gateway.prepare(call_id="save", tool_name="save", arguments={})
        with bind_tool_call_context(ToolCallContext(identity, ("save",))):
            result = gateway.execute_prepared(prepared)
    assert result.status == "completed", result.reason
    assert observed[0].identity is identity
    assert observed[0].assistant_tools == ("save",)


@pytest.mark.parametrize("nested_phase", ["prepare", "setup"])
def test_nested_gateway_during_preparation_or_setup_cannot_claim_native_dispatch(nested_phase):
    observed = []

    def inner(*, ctx: ToolContext):
        observed.append(ctx)
        return "inner"

    inner_gateway = AgentLoomToolGateway.from_tools([inner])

    def nested(*_):
        result = inner_gateway.invoke(call_id="same", tool_name="inner", arguments={})
        assert result.status == "completed", result.reason

    def outer(*, ctx: ToolContext):
        assert ctx.identity is identity
        return "outer"

    ordinary = bind_tool(outer)
    binding = ToolBinding(
        definition=ordinary.definition, inputs_schema=ordinary.inputs_schema, forward=outer,
        setup=nested if nested_phase == "setup" else None,
        input_validator=nested if nested_phase == "prepare" else None,
    )
    identity = NativeCallIdentity(
        application_id="app", task_id="task", run_id="run", instance_id="instance",
        native_session_id="session", native_parent_id="parent", call_id="same",
    )
    with execution(), bind_tool_call_context(ToolCallContext(identity, ("outer",))):
        result = AgentLoomToolGateway([binding]).invoke(call_id="same", tool_name="outer", arguments={})
    assert result.status == "completed", result.reason
    assert observed[0].identity is None
    assert observed[0].assistant_tools == ()


@pytest.mark.parametrize("reference", ["#/$defs/Args", "#Args"])
def test_local_root_reference_cannot_expose_context_parameter(reference):
    def save(value: str, *, ctx: ToolContext):
        return value

    schema = {"$defs": {"Args": {
        "$anchor": "Args", "type": "object",
        "properties": {"value": {"type": "string"}, "ctx": {"type": "object"}},
        "required": ["value", "ctx"],
    }}, "$ref": reference}
    with pytest.raises(ValueError, match="must not appear"):
        ToolBinding(definition=ToolDefinition(name="save", description="Save.", parameters=schema),
                    inputs_schema={"value": {"type": "string"}}, forward=save)


def test_nested_business_field_named_ctx_is_not_an_injected_parameter():
    def save(payload: dict, *, ctx: ToolContext):
        assert isinstance(ctx, ToolContext)
        return payload["ctx"]

    schema = {"type": "object", "properties": {"payload": {"$ref": "#/$defs/Payload"}},
              "$defs": {"Payload": {"type": "object", "properties": {"ctx": {"type": "string"}},
                                    "required": ["ctx"]}}, "required": ["payload"]}
    binding = ToolBinding(definition=ToolDefinition(name="save", description="Save.", parameters=schema),
                          inputs_schema={"payload": {"type": "object"}}, forward=save)
    with execution():
        result = AgentLoomToolGateway([binding]).invoke(
            call_id="business", tool_name="save", arguments={"payload": {"ctx": "business data"}},
        )
    assert result.status == "completed", result.reason
    assert result.output == "business data"


@pytest.mark.parametrize("callable_instance", [False, True])
def test_unknown_business_annotation_does_not_hide_resolvable_context(callable_instance):
    namespace = {"ToolContext": ToolContext}
    if callable_instance:
        exec("from __future__ import annotations\n"
             "class Save:\n"
             " name = 'save'\n"
             " def __call__(self, value: LocalPayload, *, ctx: ToolContext):\n"
             "  assert ctx.tool_name == 'save'\n"
             "  return value\n", namespace)
        tool = namespace["Save"]()
    else:
        exec("from __future__ import annotations\n"
             "def save(value: LocalPayload, *, ctx: ToolContext):\n"
             " assert ctx.tool_name == 'save'\n"
             " return value\n", namespace)
        tool = namespace["save"]
    binding = bind_tool(tool)
    assert binding.context_parameter == "ctx"
    assert set(binding.definition.parameters["properties"]) == {"value"}
    assert binding.definition.parameters["properties"]["value"]["type"] == "any"
    with execution():
        result = AgentLoomToolGateway([binding]).invoke(
            call_id="partly-resolved", tool_name="save", arguments={"value": {"content": "saved"}},
        )
    assert result.status == "completed", result.reason
    assert result.output == {"content": "saved"}


def test_unresolvable_explicit_context_annotation_fails_before_schema_publication():
    namespace = {}
    exec("from __future__ import annotations\n"
         "def save(value: LocalPayload, *, ctx: ToolContext): return value\n", namespace)
    with pytest.raises(TypeError, match="Cannot resolve ToolContext annotation"):
        bind_tool(namespace["save"])


def test_unknown_legacy_business_type_named_ctx_remains_a_model_argument():
    namespace = {}
    exec("from __future__ import annotations\n"
         "def legacy(ctx: LocalPayload): return ctx\n", namespace)
    binding = bind_tool(namespace["legacy"])
    assert binding.context_parameter is None
    assert binding.definition.parameters["properties"]["ctx"]["type"] == "any"
