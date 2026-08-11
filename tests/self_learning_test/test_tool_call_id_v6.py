from __future__ import annotations


def test_wrapped_tool_reuses_one_call_id_for_call_and_result_events() -> None:
    from src.extensions.self_learning.session_recorder import event_from_hook_context
    from src.lib.smolagents.hooks import HookHandler, HookPlan, HookRun
    from src.lib.smolagents.hooks.tool_shim import inject_hooks
    from src.lib.smolagents.hooks.types import HookEvent, HookResult
    from src.trace import ExplicitExecutionContext, bind_explicit_execution_context

    observed = []

    def capture(context):
        observed.append(context)
        return HookResult()

    run = HookRun(
        HookPlan(
            (
                HookHandler(HookEvent.PRE_TOOL_USE, "echo", capture),
                HookHandler(HookEvent.POST_TOOL_USE, "echo", capture),
            )
        ),
        local_run_id="local_1",
        root_run_id="root_1",
        agent_config={"application_id": "app"},
        project_root="/tmp",
    )

    class EchoTool:
        name = "echo"
        inputs = {"text": {"type": "string", "required": True}}

        def forward(self, text: str) -> str:
            return text

    execution = ExplicitExecutionContext(
        task_id="task_1",
        sub_task_id=None,
        agent_id="agent_1",
        agent_name="agent",
        agent_config={"application_id": "app"},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id="root_1",
        local_run_id="local_1",
    )
    tool = EchoTool()

    with bind_explicit_execution_context(execution):
        inject_hooks(tool)
        assert tool.forward(text="hello") == "hello"

    assert [context.hook_event_name for context in observed] == [
        "PreToolUse",
        "PostToolUse",
    ]
    assert observed[0].tool_call_id
    assert observed[0].tool_call_id == observed[1].tool_call_id

    events = [event_from_hook_context(context) for context in observed]
    assert [event.tool_call_id for event in events] == [
        observed[0].tool_call_id,
        observed[0].tool_call_id,
    ]
    assert [event.to_record()["tool_call_id"] for event in events] == [
        observed[0].tool_call_id,
        observed[0].tool_call_id,
    ]
