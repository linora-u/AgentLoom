"""
tests/tools_test/shell/test_shell_registry.py

ShellProcessRegistry 与 shell_tool per-agent 绑定集成测试。

背景
─────
将 ShellProcess 与 agent 实例通过 agent_id 绑定在注册表中，
使同一 Agent 的多次工具调用之间共享同一个 session-scoped shell 会话，从而保留：
  - 工作目录（cd 跨调用生效）
  - shell snapshot（aliases/functions/options/PATH）

环境变量 export 只在单条命令内生效，不跨工具调用持久化。

注册表 key = agent_id（如 "supervisor_001"、"worker_001"）
  - supervisor 和 worker 各有独立的 shell session，互不干扰

测试分四组
──────────
1. ShellProcessRegistry 单元测试
   验证单例语义、相同/不同 agent_id 的进程隔离、release 行为。

2. 无 agent 上下文（回退模式）
   验证在 agent_id 缺失时 shell_tool 仍能正常执行，且不污染注册表。

3. 有 agent 上下文（持久绑定）
   验证 shell_tool 在 agent_id 存在时注册进程并跨多次调用复用。

4. 真实 cd / cwd 隔离（集成测试）
   通过实际 shell 执行 cd 命令，验证：
     - 同一 agent_id 的 cwd 在多次调用间持久
     - supervisor 和 worker 的 shell session 完全隔离（互不干扰）

注意
─────
- _in_context() 保证每个测试函数使用独立的 contextvars 上下文，
  防止 agent_id 在测试之间泄漏。
"""

import pytest
import os
import threading
from contextvars import copy_context

from src.tools.shell.process import ShellProcess, ShellProcessRegistry
from src.tools.shell import shell_tool
from src.trace import (
    set_current_agent_id,
    clear_current_agent_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_context(fn, *args, **kwargs):
    """
    在当前 contextvars context 的隔离副本中运行 fn。

    使用 copy_context() 创建快照，保证：
      - fn 内部对 agent_id 等 contextvars 的修改不会回流到调用者
      - 不同测试之间的上下文变量不会相互污染
    """
    result_holder = []

    def _wrapper():
        result_holder.append(fn(*args, **kwargs))

    copy_context().run(_wrapper)
    return result_holder[0]


def _path_output(output: str) -> str:
    """Extract the logical path from shell output that may include login noise."""
    last_line = output.strip().splitlines()[-1].strip()
    if last_line.startswith("login: "):
        return last_line.removeprefix("login: ").strip()
    return last_line


def _logical(path: str) -> str:
    return os.path.normpath(path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """
    在每个测试前后自动清空 ShellProcessRegistry。

    autouse=True 表示本 fixture 对本文件所有测试均自动生效，无需显式声明。
    清理操作：
      - 测试前：释放上一次测试可能遗留的 shell 进程
      - 测试后：释放本次测试创建的 shell 进程（防止进程泄漏）
    """
    registry = ShellProcessRegistry.get_instance()
    for key in registry.registered_agent_ids():
        registry.release(key)
    yield
    for key in registry.registered_agent_ids():
        registry.release(key)


@pytest.fixture
def registry():
    """返回全局 ShellProcessRegistry 单例，供测试函数直接注入使用。"""
    return ShellProcessRegistry.get_instance()


# ---------------------------------------------------------------------------
# 1. ShellProcessRegistry 单元测试
# ---------------------------------------------------------------------------

def test_registry_singleton(registry):
    """
    ShellProcessRegistry 必须是单例：多次调用 get_instance() 返回同一对象。
    这确保全局只有一个注册表管理所有 agent_id → ShellProcess 的映射。
    """
    assert registry is ShellProcessRegistry.get_instance()


def test_same_agent_id_returns_same_process(registry):
    """
    对同一个 agent_id 多次调用 get_or_create()，必须返回同一个 ShellProcess 实例。
    这是 session 绑定的核心保证：同一 agent 的所有 shell 操作共享同一会话。
    """
    p1 = registry.get_or_create("supervisor_001", session_scoped=False)
    p2 = registry.get_or_create("supervisor_001", session_scoped=False)
    assert p1 is p2, "相同 agent_id 必须返回同一 ShellProcess 对象"


def test_existing_agent_process_refreshes_runtime_options(registry):
    """
    同一 agent 的 ShellProcess 对象会复用，但每次 shell_tool 调用传入的
    timeout/load_profile 等运行参数必须刷新到对象上。
    """
    p1 = registry.get_or_create(
        "timeout_agent",
        timeout=3,
        session_scoped=True,
        load_profile=False,
    )
    p2 = registry.get_or_create(
        "timeout_agent",
        timeout=60,
        session_scoped=True,
        load_profile=True,
    )

    assert p1 is p2
    assert p2.timeout == 60
    assert p2.load_profile is True


def test_different_agent_ids_return_different_processes(registry):
    """
    不同的 agent_id 必须得到各自独立的 ShellProcess 实例。
    supervisor 和 worker 各有独立 shell session，数据结构层面已隔离。
    """
    p_sup = registry.get_or_create("supervisor_001", session_scoped=False)
    p_wkr = registry.get_or_create("worker_001", session_scoped=False)
    assert p_sup is not p_wkr, "不同 agent_id 必须返回不同的 ShellProcess 对象"


def test_release_then_recreate_gives_new_process(registry):
    """
    release() 后，再次 get_or_create() 必须返回全新的 ShellProcess 对象。
    用于 agent 生命周期结束时清理旧会话，下次重新创建干净环境。
    """
    p1 = registry.get_or_create("agent_b", session_scoped=False)
    registry.release("agent_b")
    p2 = registry.get_or_create("agent_b", session_scoped=False)
    assert p1 is not p2, "release 后再次创建必须返回新的 ShellProcess 对象"


def test_release_nonexistent_is_safe(registry):
    """
    对从未注册的 agent_id 调用 release() 必须安全（不抛出异常）。
    允许调用者在不确定是否已注册的情况下安全地执行清理。
    """
    registry.release("does_not_exist")  # 不应抛出任何异常


def test_registered_agent_ids_snapshot(registry):
    """
    registered_agent_ids() 返回当前注册表中所有 agent_id 的快照。
    release 后，对应 agent_id 应从快照中消失；未 release 的应保留。
    """
    registry.get_or_create("alpha", session_scoped=False)
    registry.get_or_create("beta", session_scoped=False)

    ids = registry.registered_agent_ids()
    assert "alpha" in ids
    assert "beta" in ids

    registry.release("alpha")
    assert "alpha" not in registry.registered_agent_ids(), "release 后 agent_id 应从注册表消失"
    assert "beta" in registry.registered_agent_ids(), "未 release 的 agent_id 应仍在注册表"


# ---------------------------------------------------------------------------
# 2. shell_tool — 无 agent 上下文（回退模式）
# ---------------------------------------------------------------------------

def test_shell_tool_works_without_agent_context():
    """
    agent_id 为 None（无 agent 上下文）时，shell_tool 应当正常工作。
    此为向后兼容场景：直接调用 shell_tool（如单元测试、脚本中使用）不依赖 agent 框架。
    此时底层创建一次性 standalone 子进程，执行完毕即销毁。
    """
    def _run():
        clear_current_agent_id()
        return shell_tool("echo hello_no_agent")

    result = _in_context(_run)
    assert "hello_no_agent" in result, f"无 agent 上下文时 shell_tool 应正常执行，输出: {result!r}"


def test_fallback_does_not_pollute_registry(registry):
    """
    回退模式（agent_id 缺失）下执行 shell_tool 不应向注册表写入任何记录。
    注册表只保存有真实 agent_id 的 session-scoped 会话，一次性进程不应混入。
    """
    before = set(registry.registered_agent_ids())

    def _run():
        clear_current_agent_id()
        shell_tool("echo no_registration")

    _in_context(_run)
    assert before == set(registry.registered_agent_ids()), \
        "回退模式不应向注册表中添加任何 agent_id"


# ---------------------------------------------------------------------------
# 3. shell_tool — 有 agent 上下文（持久绑定）
# ---------------------------------------------------------------------------

def test_shell_tool_registers_process_on_first_call(registry):
    """
    在 agent 上下文中第一次调用 shell_tool 后，注册表中应出现对应的 agent_id。
    验证"首次使用时自动创建并注册"语义。
    """
    def _run():
        set_current_agent_id("reg_test_agent")
        shell_tool("echo init")

    _in_context(_run)
    assert "reg_test_agent" in registry.registered_agent_ids(), \
        "首次调用 shell_tool 后，agent_id 应出现在注册表中"


def test_same_agent_reuses_process_across_calls(registry):
    """
    同一 agent_id 在两次 shell_tool 调用之间，底层 ShellProcess 对象必须相同。
    通过比较两次从注册表取出的对象引用来验证复用行为。
    对象相同 (is) 意味着 shell session 状态（cwd、snapshot）会被保留。
    """
    captured = {}

    def _run():
        set_current_agent_id("reuse_agent")
        shell_tool("echo first")
        # 第一次调用后取出注册表中的进程对象
        captured["p1"] = registry.get_or_create("reuse_agent", session_scoped=True)
        shell_tool("echo second")
        # 第二次调用后再取出，应是同一个对象
        captured["p2"] = registry.get_or_create("reuse_agent", session_scoped=True)

    _in_context(_run)
    assert captured["p1"] is captured["p2"], \
        "同一 agent 的两次 shell_tool 调用必须复用相同的 ShellProcess 实例"


def test_agent_id_fallback_preserves_cwd_from_executor_thread(bypass_shell_security):
    """
    【真实执行】模拟 code executor 在线程中直接调用 shell_tool。

    ContextVar 不会自动传播到新线程；shell_tool 必须能通过 task_context
    的 agent_id fallback 绑定到同一个 session-scoped ShellProcess。
    """
    results = {}

    def _worker_thread():
        try:
            shell_tool("cd /tmp")
            results["pwd"] = shell_tool("pwd")
        except Exception as exc:
            results["error"] = str(exc)

    set_current_agent_id("executor_thread_agent")
    try:
        thread = threading.Thread(target=_worker_thread)
        thread.start()
        thread.join(timeout=10)
    finally:
        clear_current_agent_id()

    assert not thread.is_alive(), "executor thread should finish"
    assert "error" not in results, results.get("error")
    assert _path_output(results["pwd"]) == _logical("/tmp")


# ---------------------------------------------------------------------------
# 4. 真实 cd / cwd 隔离（实际 shell 执行集成测试）
# ---------------------------------------------------------------------------

def test_cwd_persists_within_same_agent(bypass_shell_security):
    """
    【真实执行】验证 cwd（工作目录）在同一 agent_id 的多次 shell_tool 调用间持久化。

    测试步骤：
      1. agent 执行 `cd /tmp`
      2. 同一 agent 执行 `pwd`
    期望：pwd 输出 /tmp，证明 cd 的效果跨调用保留。
    """
    def _run():
        set_current_agent_id("cwd_persist_agent")
        shell_tool("cd /tmp")       # 切换工作目录
        return shell_tool("pwd")    # 在同一 session-scoped session 中读取 cwd

    result = _in_context(_run)
    assert _path_output(result) == _logical("/tmp"), \
        f"cd /tmp 后 pwd 应返回逻辑 /tmp，实际输出: {result!r}"


def test_two_agents_have_independent_cwd(bypass_shell_security):
    """
    【真实执行】验证 supervisor 和 worker 的 shell session cwd 完全隔离。

    交替执行场景（8 步）：
    ┌──────────────┬───────────────────────┬───────────────────────┐
    │ 步骤         │ supervisor_001         │ worker_001            │
    ├──────────────┼───────────────────────┼───────────────────────┤
    │ step 1       │ cd /tmp               │                       │
    │ step 2       │                       │ cd /var/log           │
    │ step 3 (验证)│ pwd → /tmp            │                       │ ← 不受 worker 影响
    │ step 4 (验证)│                       │ pwd → /var/log        │ ← 不受 supervisor 影响
    │ step 5       │ cd /var               │                       │
    │ step 6       │                       │ cd /tmp               │
    │ step 7 (验证)│ pwd → /var            │                       │
    │ step 8 (验证)│                       │ pwd → /tmp            │
    └──────────────┴───────────────────────┴───────────────────────┘

    注意：两个 agent 各自在独立的 _in_context() 中运行，
    保证其 contextvars（agent_id）不互相覆盖。
    """
    sup_cwds = {}
    wkr_cwds = {}

    def _run_supervisor():
        set_current_agent_id("supervisor_001")
        shell_tool("cd /tmp")                              # step 1
        sup_cwds["step3"] = shell_tool("pwd")              # step 3：验证未被 worker 影响
        shell_tool("cd /var")                              # step 5
        sup_cwds["step7"] = shell_tool("pwd")              # step 7

    def _run_worker():
        set_current_agent_id("worker_001")
        shell_tool("cd /var/log")                          # step 2
        wkr_cwds["step4"] = shell_tool("pwd")              # step 4：验证未被 supervisor 影响
        shell_tool("cd /tmp")                              # step 6
        wkr_cwds["step8"] = shell_tool("pwd")              # step 8

    _in_context(_run_supervisor)
    _in_context(_run_worker)

    # --- supervisor 断言 ---
    assert _path_output(sup_cwds["step3"]) == _logical("/tmp"), \
        f"step3: supervisor cd /tmp 后 pwd 应为逻辑 /tmp，实际: {sup_cwds['step3']!r}"
    assert _path_output(sup_cwds["step3"]) != _logical("/var/log"), \
        f"step3: supervisor 不应受 worker cd /var/log 的影响，实际: {sup_cwds['step3']!r}"
    assert _path_output(sup_cwds["step7"]) == _logical("/var"), \
        f"step7: supervisor cd /var 后 pwd 应为逻辑 /var，实际: {sup_cwds['step7']!r}"

    # --- worker 断言 ---
    assert _path_output(wkr_cwds["step4"]) == _logical("/var/log"), \
        f"step4: worker cd /var/log 后 pwd 应为逻辑 /var/log，实际: {wkr_cwds['step4']!r}"
    assert _path_output(wkr_cwds["step4"]) != _logical("/tmp"), \
        f"step4: worker 不应受 supervisor cd /tmp 的影响，实际: {wkr_cwds['step4']!r}"
    assert _path_output(wkr_cwds["step8"]) == _logical("/tmp"), \
        f"step8: worker cd /tmp 后 pwd 应为逻辑 /tmp，实际: {wkr_cwds['step8']!r}"


def test_supervisor_and_worker_shells_are_independent(bypass_shell_security):
    """
    【真实执行】验证 supervisor 和 worker 从一开始就拥有完全独立的 shell 进程。

    测试步骤：
      - supervisor 执行 `cd /tmp`，然后 pwd → 应为 /tmp
      - worker 不执行任何 cd，直接 pwd → 应为系统默认目录（非 /tmp）

    期望：worker 的 cwd 与 supervisor 切换后的 cwd 不同，
    证明两个 agent 从未共享过同一个 shell 进程。
    """
    results = {}

    def _run_sup():
        set_current_agent_id("sup_independent")
        shell_tool("cd /tmp")
        results["sup"] = shell_tool("pwd")   # supervisor 已 cd 到 /tmp

    def _run_wkr():
        set_current_agent_id("wkr_independent")
        results["wkr"] = shell_tool("pwd")   # worker 从未 cd，读取初始目录

    _in_context(_run_sup)
    _in_context(_run_wkr)

    assert _path_output(results["sup"]) == _logical("/tmp"), \
        f"supervisor 应在逻辑 /tmp，实际: {results['sup']!r}"
    assert _path_output(results["sup"]) != _path_output(results["wkr"]), \
        f"worker 的 cwd 必须与 supervisor 独立，supervisor={results['sup']!r}, worker={results['wkr']!r}"
