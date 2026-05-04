import pytest

from src.trace.task_context import (
    clear_current_hook_manager,
    set_current_hook_manager,
)
from src.lib.smolagents.tools.tools import tool
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.tool_shim import inject_hooks
from src.lib.smolagents.hooks.types import HookEvent, HookResult


@pytest.fixture(autouse=True)
def reset_hook_manager():
    HookManager._instance = None
    clear_current_hook_manager()
    manager = HookManager.get_instance()
    manager.hooks = {event: [] for event in HookEvent}
    yield manager
    clear_current_hook_manager()
    HookManager._instance = None


def build_add_tool():
    @tool
    def add(a: int, b: int) -> int:
        """Add two integers.

        Args:
            a: First operand.
            b: Second operand.
        """

        return a + b

    return add


def build_query_tool():
    @tool
    def query_tool(a: int, b: int, query: str, tag: str) -> int:
        """Return the sum of two integers.

        Args:
            a: First operand.
            b: Second operand.
            query: JSON payload.
            tag: Extra marker.
        """

        return a + b

    return query_tool


def build_error_tool():
    @tool
    def explode_tool(value: int) -> int:
        """Always raises an error.

        Args:
            value: Any value.
        """

        raise ValueError(f"boom-{value}")

    return explode_tool


def test_tool_direct_call_does_not_trigger_hooks_without_injection(reset_hook_manager):
    counters = {"pre": 0, "post": 0}

    def pre(context):
        counters["pre"] += 1
        return HookResult(success=True, decision="allow")

    def post(context):
        counters["post"] += 1
        return HookResult(success=True, decision="allow")

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)
    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE, "*", post)

    add = build_add_tool()
    assert add(a=1, b=2) == 3
    assert counters == {"pre": 0, "post": 0}


def test_double_inject_is_idempotent(reset_hook_manager):
    counters = {"pre": 0, "post": 0}

    def pre(context):
        counters["pre"] += 1
        return HookResult(success=True, decision="allow")

    def post(context):
        counters["post"] += 1
        return HookResult(success=True, decision="allow")

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)
    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE, "*", post)

    add = build_add_tool()
    wrapped_once = inject_hooks(add)
    wrapped_again = inject_hooks(wrapped_once)
    assert wrapped_again is add

    assert wrapped_again(a=1, b=2) == 3
    assert counters == {"pre": 1, "post": 1}


def test_pre_modify_changes_effective_call_input(reset_hook_manager):
    def pre(context):
        return HookResult(
            success=True,
            decision="modify",
            modified_input={"a": 10, "b": 20},
        )

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == 30


def test_post_hook_exception_does_not_break_tool_result(reset_hook_manager):
    def pre(context):
        return HookResult(success=True, decision="allow")

    def post(context):
        raise RuntimeError("boom-post")

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)
    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE, "*", post)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == 3


def test_pre_hook_queues_agent_context_for_next_reasoning(reset_hook_manager):
    def pre(context):
        return HookResult(
            success=True,
            decision="allow",
            agent_context="phase-1 context",
        )

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == 3
    assert reset_hook_manager.consume_pending_agent_context() == ["phase-1 context"]
    assert reset_hook_manager.consume_pending_agent_context() == []


def test_post_hook_delivers_user_message_to_sink(reset_hook_manager):
    delivered_messages = []
    reset_hook_manager.set_user_message_sink(delivered_messages.append)

    def post(context):
        return HookResult(
            success=True,
            decision="allow",
            user_message="update task_plan.md",
        )

    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE, "*", post)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == 3
    assert delivered_messages == ["update task_plan.md"]
    assert reset_hook_manager.consume_pending_agent_context() == []


def test_pre_hook_block_takes_precedence_over_failed_flag(reset_hook_manager):
    def pre(context):
        return HookResult(success=False, decision="block", reason="invalid pre hook")

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)
    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == "invalid pre hook"


def test_post_hook_block_takes_precedence_over_failed_flag(reset_hook_manager):
    def post(context):
        return HookResult(success=False, decision="block", reason="invalid post hook")

    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE, "*", post)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == "invalid post hook"


def test_stop_check_blocks_with_reason(reset_hook_manager):
    def stop_hook(context):
        return HookResult(
            success=True,
            decision="block",
            reason="2 phases still pending",
        )

    reset_hook_manager.register_hook(HookEvent.STOP, "*", stop_hook)

    check = reset_hook_manager.build_stop_check()
    with pytest.raises(AssertionError, match="2 phases still pending"):
        check("final answer", memory=[])


def test_tool_error_keeps_original_exception_and_source(reset_hook_manager):
    def post_error(context):
        return HookResult(success=True, decision="allow")

    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE_FAILURE, "*", post_error)

    explode_tool = inject_hooks(build_error_tool())
    with pytest.raises(ValueError, match="boom-7") as exc_info:
        explode_tool(value=7)

    assert any(frame.name == "explode_tool" for frame in exc_info.traceback)


def test_query_flatten_keeps_other_fields_and_supports_mixed_args(reset_hook_manager):
    captured_inputs = []

    def pre(context):
        captured_inputs.append(dict(context.tool_input))
        return HookResult(success=True, decision="allow")

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)

    query_tool = inject_hooks(build_query_tool())
    result = query_tool(1, b=2, query='{"inner":"x","a":10}', tag="outside")
    assert result == 3

    assert len(captured_inputs) == 1
    seen = captured_inputs[0]
    assert seen["a"] == 10
    assert seen["b"] == 2
    assert seen["tag"] == "outside"
    assert seen["inner"] == "x"
    assert "query" not in seen


def test_injected_tool_prefers_current_hook_manager_context(reset_hook_manager):
    counters = {"ctx_a": 0, "ctx_b": 0, "global": 0}

    def pre_global(context):
        counters["global"] += 1
        return HookResult(success=True, decision="allow")

    def pre_a(context):
        counters["ctx_a"] += 1
        return HookResult(success=True, decision="allow")

    def pre_b(context):
        counters["ctx_b"] += 1
        return HookResult(success=True, decision="allow")

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre_global)

    manager_a = HookManager()
    manager_a.register_hook(HookEvent.PRE_TOOL_USE, "*", pre_a)
    manager_b = HookManager()
    manager_b.register_hook(HookEvent.PRE_TOOL_USE, "*", pre_b)

    add = inject_hooks(build_add_tool())

    set_current_hook_manager(manager_a)
    assert add(a=1, b=2) == 3

    set_current_hook_manager(manager_b)
    assert add(a=2, b=3) == 5

    clear_current_hook_manager()
    assert add(a=3, b=4) == 7

    assert counters == {"ctx_a": 1, "ctx_b": 1, "global": 1}


def test_pre_hook_queues_agent_context_for_next_model_turn(reset_hook_manager):
    def pre(context):
        return HookResult(
            success=True,
            decision="allow",
            agent_context="phase-1 still active",
        )

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == 3
    assert reset_hook_manager.consume_pending_agent_context() == ["phase-1 still active"]
    assert reset_hook_manager.consume_pending_agent_context() == []


def test_post_hook_queues_user_message_for_transcript(reset_hook_manager):
    delivered_messages = []
    reset_hook_manager.set_user_message_sink(delivered_messages.append)

    def post(context):
        return HookResult(
            success=True,
            decision="allow",
            user_message="[agent-recall-with-files] File updated.",
        )

    reset_hook_manager.register_hook(HookEvent.POST_TOOL_USE, "*", post)

    add = inject_hooks(build_add_tool())
    assert add(a=1, b=2) == 3
    assert delivered_messages == ["[agent-recall-with-files] File updated."]
    assert reset_hook_manager.consume_pending_user_messages() == []


def test_legacy_message_constructor_field_is_removed(reset_hook_manager):
    with pytest.raises(TypeError):
        HookResult(success=True, decision="allow", message="legacy-message")


def test_legacy_print_message_constructor_field_is_removed(reset_hook_manager):
    with pytest.raises(TypeError):
        HookResult(success=True, decision="allow", print_message="legacy-print")


def test_legacy_return_message_constructor_field_is_removed(reset_hook_manager):
    with pytest.raises(TypeError):
        HookResult(success=True, decision="allow", return_message="legacy-return")


@pytest.mark.parametrize("decision", ["retry", "continue"])
def test_unsupported_hook_decision_is_rejected(reset_hook_manager, decision):
    with pytest.raises(ValueError, match=decision):
        HookResult(success=True, decision=decision)


# ── modified_input 合并覆盖测试 ──────────────────────────────────────


def test_modify_partial_input_preserves_unmentioned_fields(reset_hook_manager):
    """单 Hook 场景：modified_input 只写要改的字段，未指定的字段应保留原值。"""

    def pre(context):
        # 只修改 a，不提 b
        return HookResult(success=True, decision="modify", modified_input={"a": 100})

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", pre)

    add = inject_hooks(build_add_tool())
    # 原始调用 add(a=1, b=2)，Hook 把 a 改成 100，b 应保留为 2
    assert add(a=1, b=2) == 102  # 100 + 2


def test_chained_modify_hooks_merge_incrementally(reset_hook_manager):
    """多 Hook 链场景：两个 Hook 分别修改不同字段，最终结果应包含两者的修改。"""

    def hook_change_a(context):
        # 第一个 Hook：只改 a
        return HookResult(success=True, decision="modify", modified_input={"a": 50})

    def hook_change_b(context):
        # 第二个 Hook：只改 b
        return HookResult(success=True, decision="modify", modified_input={"b": 30})

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook_change_a)
    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook_change_b)

    add = inject_hooks(build_add_tool())
    # 原始 add(a=1, b=2)
    # Hook A: a=50, b 保留 → {a: 50, b: 2}
    # Hook B: b=30, a 保留 → {a: 50, b: 30}
    assert add(a=1, b=2) == 80  # 50 + 30


def test_chained_modify_hooks_later_overrides_same_field(reset_hook_manager):
    """多 Hook 链场景：两个 Hook 修改同一字段，后注册的 Hook 值生效。"""

    def hook_a_to_10(context):
        return HookResult(success=True, decision="modify", modified_input={"a": 10})

    def hook_a_to_99(context):
        return HookResult(success=True, decision="modify", modified_input={"a": 99})

    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook_a_to_10)
    reset_hook_manager.register_hook(HookEvent.PRE_TOOL_USE, "*", hook_a_to_99)

    add = inject_hooks(build_add_tool())
    # 第一个 Hook 把 a 改成 10，第二个又改成 99，b 始终保留原值 2
    assert add(a=1, b=2) == 101  # 99 + 2
