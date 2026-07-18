import { afterEach, describe, expect, test } from "bun:test"
import type { ScrollBoxRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"
import type {
  BootstrapResultDto,
  RpcMethod,
  RpcParams,
  RpcResult,
  RunDetailResultDto,
  RuntimeSummaryDto,
  SystemDetailResultDto,
} from "../../src/domain"
import { AgentLoomApp } from "../../src/app/view"
import { AgentLoomSession, type TuiClient } from "../../src/app/session"
import { buildPaletteItems } from "../../src/app/controller"

const PAGE_DOWN_KEY = "\x1B[6~"

const snapshot: BootstrapResultDto = {
  project: { root: "/repo", name: "repo" },
  models: {
    default: "powerful",
    configured: true,
    items: [
      { type: "powerful", description: "Best quality", default: true, configured: true },
      { type: "fast", description: "Lower latency", default: false, configured: true },
    ],
  },
  systems: [
    {
      id: "applications/new/workflows/new.yaml",
      path: "applications/new/workflows/new.yaml",
      application_id: "new",
      name: "new_agent",
      description: "not run",
      state: "never_run",
      validation: { valid: true, errors: [] },
      latest_run: null,
    },
  ],
  runs: [],
  worker_invocations: [],
  worker_invocations_incomplete: false,
  applications: [],
  agents: [],
  skills: [],
  schedules: {
    items: [],
    service: {
      state: "stopped",
      pid: null,
      started_at: null,
      last_tick_at: null,
      last_success_at: null,
      last_error: null,
      job_count: 0,
      due_count: 0,
      claimed_count: 0,
      execution_count: 0,
    },
  },
}

const longSnapshot: BootstrapResultDto = {
  ...snapshot,
  systems: Array.from({ length: 24 }, (_, index) => {
    const suffix = String(index).padStart(2, "0")
    return {
      ...snapshot.systems[0]!,
      id: `applications/app-${suffix}/workflows/agent-${suffix}.yaml`,
      path: `applications/app-${suffix}/workflows/agent-${suffix}.yaml`,
      application_id: `app-${suffix}`,
      name: `agent_${suffix}`,
    }
  }),
}

const renderers: Array<{ destroy(): void }> = []

const systemDetail: SystemDetailResultDto = {
  summary: snapshot.systems[0]!,
  definition: {
    name: "new_agent",
    description: "not run",
    workflow: "answer",
    model_type: "powerful",
    path: "applications/new/workflows/new.yaml",
  },
  files: [],
  topology: {
    supervisor: { name: "new_agent", path: "applications/new/workflows/new.yaml" },
    workers: [],
  },
  execution: { state: "never_run", latest_run: null },
  result_state: "never_run",
}

const catalogSnapshot: BootstrapResultDto = {
  ...snapshot,
  worker_invocations: [{
    run_id: "run-helper",
    system_id: "applications/new/workflows/new.yaml",
    application_id: "new",
    parent_agent_name: "new_agent",
    agent_name: "helper",
    call_index: 1,
    status: "failed",
    step: 2,
    started_at: "2026-07-18T10:00:00Z",
    ended_at: "2026-07-18T10:00:05Z",
    error: "helper failed",
  }],
  applications: [{
    id: "new",
    name: "new",
    path: "applications/new",
    system_count: 1,
    worker_count: 1,
    skill_count: 1,
    run_count: 0,
    active_run_count: 0,
  }],
  agents: [{
    id: "applications/new/workflows/new.yaml",
    application_id: "new",
    name: "new_agent",
    description: "not run",
    path: "applications/new/workflows/new.yaml",
    role: "supervisor",
    skills: { load_mode: null, items: [] },
    workers: [{
      id: "applications/new/workflows/worker_agents/helper.yaml",
      application_id: "new",
      name: "helper",
      description: "assist the supervisor",
      path: "applications/new/workflows/worker_agents/helper.yaml",
      role: "worker",
      skills: { load_mode: "selected", items: ["helper-skill"] },
      workers: [],
    }],
  }],
  skills: [{
    id: "new:helper-skill",
    application_id: "new",
    name: "helper-skill",
    description: "Help with the task",
    origin: "application",
    path: "applications/new/skills/helper-skill/SKILL.md",
  }],
  schedules: {
    items: [{
      id: "new-hourly",
      name: "new-hourly",
      enabled: true,
      state: "scheduled",
      yaml_path: "applications/new/workflows/new.yaml",
      trigger: { kind: "interval", seconds: 3600, timezone: "UTC" },
      next_run_at: "2026-07-18T12:00:00Z",
      last_run_at: null,
      last_status: null,
      run_count: 0,
      last_execution: null,
    }],
    service: { ...snapshot.schedules.service, state: "running", job_count: 1 },
  },
}

const clickableRun = {
  run_id: "run-click",
  system_id: "applications/new/workflows/new.yaml",
  application_id: "new",
  task_id: "task-click",
  agent_name: "new_agent",
  status: "completed" as const,
  started_at: "2026-07-18T10:00:00Z",
  ended_at: "2026-07-18T10:01:00Z",
}

const runDetail: RunDetailResultDto = {
  summary: clickableRun,
  error: null,
  workers: [],
  events: Array.from({ length: 80 }, (_, index) => ({ type: "step", index })),
  logs: [],
  artifacts: [],
  result_state: "available",
  result: "done",
  limits: {
    workers: { truncated: false, returned_count: 0, max_count: 256 },
    events: {
      truncated: false,
      source_incomplete: false,
      returned_count: 80,
      returned_bytes: 2048,
      max_count: 200,
      max_bytes: 262_144,
      max_scan_bytes: 524_288,
    },
    logs: {
      truncated: false,
      returned_count: 0,
      returned_bytes: 0,
      max_count: 16,
      max_bytes: 262_144,
      max_bytes_per_file: 32_768,
      max_scanned_entries: 2048,
    },
    artifacts: { truncated: false, returned_count: 0, max_count: 256, max_scanned_entries: 2048 },
    result: { truncated: false, source_incomplete: false, returned_bytes: 4, max_bytes: 131_072 },
  },
}

afterEach(() => {
  for (const renderer of renderers.splice(0)) renderer.destroy()
})

describe("AgentLoom TUI view", () => {
  test("wide terminals show general chat and the clickable system directory together", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("AgentLoom Chat")
    expect(frame).toContain("普通对话")
    expect(frame).toContain("Models: powerful* · fast")
    expect(frame).toContain("项目总览")
    expect(frame).toContain("0 Applications · 1 Agents")
    expect(frame).toContain("Ctrl+P")
    expect(frame).toContain("/apply")
  })

  test("Workspace makes discovered Skills visible and directly clickable", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return catalogSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: catalogSnapshot, sessionID: "skill-list-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("已发现 1 个 Skill")
    expect(frame).toContain("helper-skill")
    expect(frame).toContain("new · 点击查看")

    const rows = frame.split("\n")
    const y = rows.findIndex((row) => row.includes("helper-skill"))
    const x = y < 0 ? -1 : rows[y]!.indexOf("helper-skill")
    expect({ x, y }).not.toEqual({ x: -1, y: -1 })
    await setup.mockMouse.click(x + 2, y)
    await Bun.sleep(20)
    await setup.renderOnce()

    expect(session.state.route).toEqual({ type: "skill", skillID: "new:helper-skill" })
    expect(setup.captureCharFrame()).toContain("helper-skill/SKILL.md")
  })

  test("Workspace separates explicit Run outcomes instead of combining or inferring them", async () => {
    const statuses = ["completed", "failed", "crashed", "interrupted", "running", "unknown"] as const
    const runs = statuses.map((status, index) => ({
      ...clickableRun,
      run_id: `run-${status}`,
      task_id: `task-${status}`,
      status,
      started_at: `2026-07-18T10:0${index}:00Z`,
      ended_at: status === "running" ? null : `2026-07-18T10:0${index}:05Z`,
    }))
    const statusSnapshot: BootstrapResultDto = { ...snapshot, systems: [], runs }
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return statusSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: statusSnapshot, sessionID: "status-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("6 次 · 1 成功 · 1 失败")
    expect(frame).toContain("1 崩溃 · 1 中断 · 1 运行中")
    expect(frame).toContain("1 状态未知")
    expect(frame).not.toContain("failed/crashed")
  })

  test("Workspace reports when Worker invocation history is incomplete", async () => {
    const incompleteSnapshot: BootstrapResultDto = {
      ...snapshot,
      worker_invocations_incomplete: true,
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return incompleteSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: incompleteSnapshot,
      sessionID: "incomplete-worker-test",
    })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("Worker 状态索引不完整")
    expect(frame).toContain("部分历史调用无法确认")
    expect(frame).toContain("Never run")
  })

  test("Workspace reports when the bounded Run index is still converging", async () => {
    const runtimeSummary: RuntimeSummaryDto = {
      systems: snapshot.systems,
      runs: [],
      runs_incomplete: true,
      removed_runs: [],
      worker_invocations: [],
      worker_invocations_incomplete: false,
      schedules: snapshot.schedules,
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return runtimeSummary as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "incomplete-runs-test" })
    await session.refreshLive()
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("Run 状态索引正在收敛")
    expect(frame).toContain("有界增量窗口")
  })

  test("shows an animated indexing phase while bootstrap runs after first render", async () => {
    const client: TuiClient = {
      request<Method extends RpcMethod>() {
        return new Promise<RpcResult<Method>>(() => {})
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, projectRoot: "/repo", sessionID: "chat-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 120, height: 28 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    void session.start()
    await setup.renderOnce()

    expect(setup.captureCharFrame()).toContain("正在索引项目")
    expect(setup.captureCharFrame()).toContain("AgentLoom Chat")
  })

  test("command palette opens one Agent in the context panel without replacing chat", async () => {
    let detailCalls = 0
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method === "system.detail") {
          detailCalls += 1
          return systemDetail as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    setup.mockInput.pressKey("p", { ctrl: true })
    await Bun.sleep(5)
    await setup.mockInput.typeText("new_agent")
    setup.mockInput.pressEnter()
    await Bun.sleep(50)
    await setup.renderOnce()

    expect(session.state.route).toEqual({
      type: "system",
      systemID: "applications/new/workflows/new.yaml",
    })
    expect(detailCalls).toBe(1)
    expect(setup.captureCharFrame()).toContain("AgentLoom Chat")
    expect(setup.captureCharFrame()).toContain("Agent")
  })

  test("catalog palette opens Application, Worker, Skill, and Schedule details", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return catalogSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: catalogSnapshot, sessionID: "catalog-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const open = async (query: string) => {
      setup.mockInput.pressKey("p", { ctrl: true })
      await Bun.sleep(5)
      await setup.mockInput.typeText(query)
      setup.mockInput.pressEnter()
      await Bun.sleep(20)
      await setup.renderOnce()
      return setup.captureCharFrame()
    }

    expect(await open("helper")).toContain("Worker Agent")
    expect(setup.captureCharFrame()).toContain("Agent 状态: failed")
    setup.mockInput.pressEscape()
    await Bun.sleep(50)
    await setup.renderOnce()
    expect(session.state.route).toEqual({ type: "builder" })

    expect(await open("helper-skill")).toContain("helper-skill/SKILL.md")
    setup.mockInput.pressKey("b")
    await setup.renderOnce()
    expect(session.state.route).toEqual({ type: "builder" })

    expect(await open("new-hourly")).toContain("Schedule")
    expect(setup.captureCharFrame()).toContain("调度服务: running")
    setup.mockInput.pressEscape()
    await Bun.sleep(50)
    await setup.renderOnce()

    expect(await open("new")).toContain("Application")
    expect(setup.captureCharFrame()).toContain("helper (worker)")
  })

  test("Recent runs are clickable and open a compact detail instead of raw events", async () => {
    const runSnapshot: BootstrapResultDto = {
      ...catalogSnapshot,
      systems: [{ ...catalogSnapshot.systems[0]!, state: "completed", latest_run: clickableRun }],
      runs: [clickableRun],
      applications: [{ ...catalogSnapshot.applications[0]!, run_count: 1 }],
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method === "run.detail") return runDetail as RpcResult<Method>
        return runSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: runSnapshot, sessionID: "run-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 36, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame().split("\n")
    const y = frame.findIndex((row) => row.includes("new_agent"))
    const x = y < 0 ? -1 : frame[y]!.indexOf("new_agent")
    expect({ x, y }).not.toEqual({ x: -1, y: -1 })
    await setup.mockMouse.click(x + 2, y)
    await Bun.sleep(20)
    await setup.renderOnce()

    expect(session.state.route.type).toBe("run")
    expect(setup.captureCharFrame()).toContain("done")
    expect(setup.captureCharFrame()).not.toContain('"index"')
    expect(setup.captureCharFrame()).not.toContain("Events")

    setup.mockInput.pressEscape()
    await Bun.sleep(50)
    await setup.renderOnce()
    expect(session.state.route).toEqual({ type: "builder" })
  })

  test("failed Run offers a working keyboard AI diagnosis action", async () => {
    const failedRun = { ...clickableRun, run_id: "run-failed", task_id: "task-failed", status: "failed" as const }
    const failedDetail: RunDetailResultDto = {
      ...runDetail,
      summary: failedRun,
      error: "provider timed out",
      events: [{ type: "internal", secret: "EVENT_SECRET" }],
      logs: [{
        path: "logs/run.log",
        size: 32,
        tail: "[ERROR] provider timed out",
        tail_truncated: false,
      }],
      result_state: "unavailable",
      result: null,
    }
    const messages: string[] = []
    const failedSnapshot: BootstrapResultDto = { ...snapshot, systems: [], runs: [failedRun] }
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "run.detail") return failedDetail as RpcResult<Method>
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "diagnose-view-test",
            assistant: "根因可能是模型服务超时。",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return failedSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: failedSnapshot,
      sessionID: "diagnose-view-test",
    })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32 },
    )
    renderers.push(setup.renderer)
    await session.openEntry(session.entries[0]!)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("[ a AI 分析原因 ]")

    setup.mockInput.pressKey("a")
    await Bun.sleep(30)
    await setup.renderOnce()

    expect(messages).toHaveLength(1)
    expect(messages[0]).toContain("provider timed out")
    expect(messages[0]).not.toContain("EVENT_SECRET")
    expect(session.state.route).toEqual({ type: "builder" })
    expect(setup.captureCharFrame()).toContain("根因可能是模型服务超时。")
  })

  test("PageDown and the mouse wheel scroll Workspace while chat remains open", async () => {
    const runs = Array.from({ length: 12 }, (_, index) => ({
      ...clickableRun,
      run_id: `run-workspace-${String(index).padStart(2, "0")}`,
      task_id: `task-workspace-${index}`,
      started_at: `2026-07-18T${String(10 + Math.floor(index / 6)).padStart(2, "0")}:${String(index % 6).padStart(2, "0")}:00Z`,
    }))
    const workspaceSnapshot: BootstrapResultDto = {
      ...catalogSnapshot,
      runs,
      systems: [{ ...catalogSnapshot.systems[0]!, state: "completed", latest_run: runs.at(-1)! }],
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return workspaceSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: workspaceSnapshot,
      sessionID: "workspace-scroll-test",
    })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 16 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const overview = setup.renderer.root.findDescendantById(
      "agentloom-workspace-scrollbox",
    ) as ScrollBoxRenderable
    expect(overview.scrollTop).toBe(0)
    expect(overview.scrollHeight).toBeGreaterThan(overview.height)

    setup.mockInput.pressKey(PAGE_DOWN_KEY)
    await Bun.sleep(10)
    await setup.renderOnce()

    expect(session.state.route).toEqual({ type: "builder" })
    expect(overview.scrollTop).toBeGreaterThan(0)

    overview.scrollTo(0)
    await setup.renderOnce()
    expect(overview.scrollTop).toBe(0)
    for (let index = 0; index < 3; index += 1) {
      await setup.mockMouse.scroll(120, 8, "down")
    }
    await setup.renderOnce()
    expect(overview.scrollTop).toBeGreaterThan(0)
  })

  test("plain Enter captures and submits the focused Builder input", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "builder-test",
            assistant: "I am the configured model.",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    await setup.mockInput.typeText("what model")
    setup.mockInput.pressEnter()
    await Bun.sleep(10)
    await setup.mockInput.typeText("next draft")
    await Bun.sleep(20)
    await setup.renderOnce()

    expect(messages).toEqual(["what model"])
    expect(setup.captureCharFrame()).not.toContain("Draft revision 0")
  })

  test("/models opens a keyboard-selectable model picker", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "model-picker-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 120, height: 28 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    await setup.mockInput.typeText("/models")
    setup.mockInput.pressEnter()
    await Bun.sleep(20)
    await setup.renderOnce()

    expect(setup.captureCharFrame()).toContain("模型选择")
    expect(setup.captureCharFrame()).toContain("powerful")
    expect(setup.captureCharFrame()).toContain("fast")

    setup.mockInput.pressArrow("down")
    setup.mockInput.pressEnter()
    await Bun.sleep(20)

    expect(session.state.modelType).toBe("fast")
    expect(session.state.notice).toBe("已切换模型: fast")
  })

  test("waits for the final CJK IME composition before submitting Enter", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "builder-test",
            assistant: "我是 AgentLoom。",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, kittyKeyboard: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    // A CJK IME can deliver Return/onSubmit before its final composed text is
    // flushed into the textarea. This ordering reproduces that terminal race.
    setup.mockInput.pressEnter()
    await setup.mockInput.typeText("你是什么模型")
    await Bun.sleep(20)

    expect(messages).toEqual(["你是什么模型"])
    expect(setup.renderer.currentFocusedEditor?.plainText).toBe("")
  })

  test("clicking the visible send control submits the current Chinese prompt", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "builder-test",
            assistant: "I am the configured model.",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    await setup.mockInput.typeText("你是什么模型")
    const rows = setup.captureCharFrame().split("\n")
    const y = rows.findIndex((row) => row.includes("Enter 发送"))
    const x = y >= 0 ? rows[y]!.indexOf("Enter 发送") : -1
    expect({ x, y }).not.toEqual({ x: -1, y: -1 })
    await setup.mockMouse.click(x + 3, y)
    await Bun.sleep(20)

    expect(messages).toEqual(["你是什么模型"])
  })

  test("Enter submits the current prompt after the editor loses focus", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "builder-test",
            assistant: "Focus recovered.",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    await setup.mockInput.typeText("recover focus")
    setup.renderer.currentFocusedEditor?.blur()
    setup.mockInput.pressEnter()
    await Bun.sleep(20)

    expect(messages).toEqual(["recover focus"])
  })

  test("modified Enter does not submit after the editor loses focus", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, kittyKeyboard: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    await setup.mockInput.typeText("do not submit")
    setup.renderer.currentFocusedEditor?.blur()
    setup.mockInput.pressEnter({ ctrl: true })
    await Bun.sleep(20)

    expect(messages).toEqual([])
  })

  test("Shift+Enter inserts a newline without submitting", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "builder-test",
            assistant: "Draft received.",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true, kittyKeyboard: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    await Bun.sleep(5)

    await setup.mockInput.typeText("first line")
    setup.mockInput.pressEnter({ shift: true })
    await setup.mockInput.typeText("second line")
    expect(messages).toEqual([])

    setup.mockInput.pressEnter()
    await Bun.sleep(20)

    expect(messages).toEqual(["first line\nsecond line"])
  })

  test("mouse wheel scrolls the searchable command palette", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return longSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: longSnapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 24, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    setup.mockInput.pressKey("p", { ctrl: true })
    await Bun.sleep(5)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("返回对话")
    for (let index = 0; index < 4; index += 1) {
      await setup.mockMouse.scroll(70, 11, "down")
    }
    await setup.renderOnce()

    const palette = setup.renderer.root.findDescendantById(
      "agentloom-palette-scrollbox",
    ) as ScrollBoxRenderable
    expect(palette.scrollTop).toBeGreaterThan(0)
  })

  test("keyboard selection keeps a deep palette entry inside the viewport", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return longSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: longSnapshot, sessionID: "builder-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 24, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    setup.mockInput.pressKey("p", { ctrl: true })
    await Bun.sleep(5)
    const targetIndex = buildPaletteItems(longSnapshot).findIndex((item) => item.title === "agent_23")
    expect(targetIndex).toBeGreaterThan(0)
    for (let index = 0; index < targetIndex; index += 1) {
      setup.mockInput.pressArrow("down")
    }
    await setup.renderOnce()

    expect(setup.renderer.root.findDescendantById(`agentloom-palette-entry-${targetIndex}`)).toBeDefined()
    const palette = setup.renderer.root.findDescendantById(
      "agentloom-palette-scrollbox",
    ) as ScrollBoxRenderable
    expect(palette).toBeDefined()
    expect(palette.scrollTop).toBeGreaterThan(0)
    expect(setup.captureCharFrame()).toContain("agent_23")
  })
})
