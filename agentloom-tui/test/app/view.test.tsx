import { afterEach, describe, expect, test } from "bun:test"
import type { ScrollBoxRenderable, TextRenderable } from "@opentui/core"
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
import {
  AgentLoomSession,
  type StudioClient,
  type StudioEvent,
  type TuiClient,
} from "../../src/app/session"
import { buildPaletteItems } from "../../src/app/controller"
import type { UpdateClient } from "../../src/update/source-updater"

const PAGE_DOWN_KEY = "\x1B[6~"
const PAGE_UP_KEY = "\x1B[5~"

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
  test("offers an explicit whole-product update and requests a safe restart", async () => {
    const actions: string[] = []
    const updater: UpdateClient = {
      async check() {
        return { available: true, sourceRoot: "/trusted/source", installedAt: 1_000, latestSourceMtime: 2_000 }
      },
      async install() { actions.push("install") },
    }
    const session = new AgentLoomSession({
      client: { async request() { return catalogSnapshot as never }, close() {} },
      snapshot: catalogSnapshot,
      updater,
    })
    await session.start()
    await Bun.sleep(0)
    const setup = await testRender(
      () => (
        <AgentLoomApp
          session={session}
          projectRoot="/repo"
          onExit={() => {}}
          onRestart={() => actions.push("restart")}
          refreshIntervalMs={0}
        />
      ),
      { width: 140, height: 32 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("发现可用更新 · Ctrl+X")

    setup.mockInput.pressKey("x", { ctrl: true })
    await Bun.sleep(5)
    await setup.mockInput.typeText("update")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("更新 AgentLoom 并安全重启")
    setup.mockInput.pressEnter()
    await Bun.sleep(20)

    expect(actions).toEqual(["install", "restart"])
  })

  test("renders a blocking OpenCode permission card and handles its keyboard choices", async () => {
    const replies: string[] = []
    let studioListener: ((event: StudioEvent) => void) | undefined
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_permission", messages: [] } },
      async send() { return { messages: [] } },
      subscribe(listener) {
        studioListener = listener
        return () => { studioListener = undefined }
      },
      async replyPermission(requestID, reply) { replies.push(`${requestID}:${reply}`) },
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method !== "application.detail") throw new Error(`unexpected ${method}`)
        return {
          schema_version: 1,
          application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
          working_revision: "sha256:working",
          running_revision: null,
          agents: [],
        } as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 36, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)

    studioListener?.({
      type: "permission",
      sessionID: "ses_permission",
      requestID: "per_bash",
      permission: "bash",
      patterns: ["loom run applications/new/workflows/new.yaml"],
      metadata: {},
    })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("需要授权 · bash")
    expect(setup.captureCharFrame()).toContain("1 仅本次")
    expect(setup.captureCharFrame()).toContain("2 本次会话")
    expect(setup.captureCharFrame()).toContain("3 拒绝")

    setup.mockInput.pressKey("2")
    await Bun.sleep(10)
    expect(replies).toEqual(["per_bash:always"])
    expect(session.state.permissionRequest).toBeNull()
  })

  test("renders completed Studio tools before the final assistant answer", async () => {
    let studioListener: ((event: StudioEvent) => void) | undefined
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_order", messages: [] } },
      async send() {
        studioListener?.({
          type: "tool",
          sessionID: "ses_order",
          callID: "call_detail",
          name: "agentloom_domain",
          title: "AgentLoom application.detail",
          status: "completed",
          output: "browser_harness_probe · healthy",
        })
        return {
          messages: [
            { id: "msg_user", role: "user", content: "inspect the Application" },
            { id: "msg_assistant", role: "assistant", content: "FINAL_ANSWER_AFTER_TOOL" },
          ],
        }
      },
      subscribe(listener) {
        studioListener = listener
        return () => { studioListener = undefined }
      },
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method !== "application.detail") throw new Error(`unexpected ${method}`)
        return {
          schema_version: 1,
          application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
          working_revision: "sha256:working",
          running_revision: null,
          agents: [],
        } as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)
    await session.submit("inspect the Application")
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 40 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("AgentLoom application.detail")
    expect(frame).toContain("FINAL_ANSWER_AFTER_TOOL")
    expect(frame.indexOf("AgentLoom application.detail")).toBeLessThan(frame.indexOf("FINAL_ANSWER_AFTER_TOOL"))
  })

  test("renders an OpenCode question card and answers an option by click", async () => {
    const replies: Array<{ requestID: string; answers: string[][] }> = []
    let studioListener: ((event: StudioEvent) => void) | undefined
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_question", messages: [] } },
      async send() { return { messages: [] } },
      subscribe(listener) {
        studioListener = listener
        return () => { studioListener = undefined }
      },
      async replyQuestion(requestID, answers) { replies.push({ requestID, answers }) },
      async rejectQuestion() {},
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method !== "application.detail") throw new Error(`unexpected ${method}`)
        return {
          schema_version: 1,
          application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
          working_revision: "sha256:working",
          running_revision: null,
          agents: [],
        } as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 36, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)

    studioListener?.({
      type: "question",
      sessionID: "ses_question",
      requestID: "que_topology",
      questions: [{
        header: "Topology",
        question: "Choose a topology",
        options: [
          { label: "Supervisor + Workers", description: "Delegate work" },
          { label: "Single Agent", description: "Keep it simple" },
        ],
        multiple: false,
        custom: true,
      }],
    })
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("需要你的决定 · Topology")
    expect(setup.captureCharFrame()).toContain("Supervisor + Workers")
    expect(setup.captureCharFrame()).toContain("输入自定义答案")

    const rows = setup.captureCharFrame().split("\n")
    const y = rows.findIndex((row) => row.includes("Supervisor + Workers"))
    const x = y >= 0 ? rows[y]!.indexOf("Supervisor + Workers") : -1
    expect({ x, y }).not.toEqual({ x: -1, y: -1 })
    await setup.mockMouse.click(x + 3, y)
    await Bun.sleep(10)
    expect(replies).toEqual([{ requestID: "que_topology", answers: [["Supervisor + Workers"]] }])
  })

  test("Application detail shows effective capabilities and keeps chat visible", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method !== "application.detail") throw new Error(`unexpected ${method}`)
        return {
          schema_version: 1,
          application: {
            id: "new",
            name: "new",
            path: "applications/new",
            health: "healthy",
            updated_at: "2026-07-20T10:00:00Z",
          },
          working_revision: "sha256:working123",
          running_revision: null,
          agents: [{
            id: "applications/new/workflows/new.yaml",
            name: "new_agent",
            description: "not run",
            role: "supervisor",
            workflow: "coordinate research",
            model: { type: "powerful", source: "agent" },
            tools: [{ name: "web_search", source: "agent" }],
            skills: [{
              name: "local-writer",
              description: "Writes reports",
              source: "agent",
              load_mode: "eager",
              path: "applications/new/skills/local-writer/SKILL.md",
            }],
            permissions: { value: { mode: "denylist" }, source: "application", source_path: "applications/new/config/system.yaml" },
            hooks: { value: {}, source: "global", source_path: "config/system.yaml" },
            mcp: { value: ["reports-db"], source: "application", source_path: "applications/new/config/system.yaml" },
            source_path: "applications/new/workflows/new.yaml",
            validation: { valid: true, errors: [] },
            workers: [{
              id: "applications/new/workflows/worker_agents/researcher.yaml",
              name: "researcher",
              description: "Finds facts",
              role: "worker",
              workflow: "research",
              model: { type: "powerful", source: "global" },
              tools: [],
              skills: [],
              permissions: { value: { mode: "denylist" }, source: "application", source_path: "applications/new/config/system.yaml" },
              hooks: { value: {}, source: "global", source_path: "config/system.yaml" },
              mcp: { value: ["reports-db"], source: "application", source_path: "applications/new/config/system.yaml" },
              source_path: "applications/new/workflows/worker_agents/researcher.yaml",
              validation: { valid: true, errors: [] },
              workers: [],
            }],
          }],
        } as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: catalogSnapshot, sessionID: "effective-detail" })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 150, height: 38, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("Working Revision")
    expect(frame).toContain("working123")
    expect(frame).toContain("local-writer · agent · eager")
    expect(frame).toContain("权限: application 2")
    expect(frame).toContain("Agents: 2 · 1 Supervisor · 1 Worker")
    expect(frame).not.toContain("coordinate research")
    expect(frame).toContain("描述你要创建或修改的 Application")
    expect(setup.renderer.currentFocusedEditor).not.toBeNull()
  })

  test("startup is Application-first instead of Run-first", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return catalogSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: catalogSnapshot,
      sessionID: "application-first-test",
    })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain("Application Studio")
    expect(frame).toContain("+ New Application")
    expect(frame).toContain("1 Application · 0 Global Skills")
    expect(frame).toContain("new")
    expect(frame).toContain("配置有效 · 尚未运行 · 点击打开")
    expect(frame).not.toContain("never run")
    expect(frame).not.toContain("1 Agents")
    expect(frame).not.toContain("/apply")
    expect(frame).not.toContain("F6")
  })

  test("shows the full nested Application ID on the homepage and in Ctrl+X", async () => {
    const applicationID = "memory_feature_validation/variants/on"
    const nestedSnapshot: BootstrapResultDto = {
      ...catalogSnapshot,
      systems: [{
        ...catalogSnapshot.systems[0]!,
        id: `applications/${applicationID}/workflows/recall.yaml`,
        path: `applications/${applicationID}/workflows/recall.yaml`,
        application_id: applicationID,
        name: "recall",
      }],
      applications: [{
        ...catalogSnapshot.applications[0]!,
        id: applicationID,
        name: "on",
        path: `applications/${applicationID}`,
      }],
      agents: [],
      skills: [],
      worker_invocations: [],
    }
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return nestedSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot: nestedSnapshot, sessionID: "nested-application-test" })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const applicationLabel = setup.renderer.root.findDescendantById(
      "agentloom-application-entry-0",
    ) as TextRenderable
    expect(applicationLabel.plainText).toBe(applicationID)
    expect(setup.captureCharFrame()).toContain("配置有效 · 尚未运行")

    setup.mockInput.pressKey("x", { ctrl: true })
    await Bun.sleep(5)
    await setup.mockInput.typeText("variants/on")
    await setup.renderOnce()

    const frame = setup.captureCharFrame()
    expect(frame).toContain(applicationID)
    expect(frame).toContain("配置有效 · 尚未运行")
    expect(frame).not.toContain("never run")
  })

  test("New Application starts a bound creation conversation", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return catalogSnapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: catalogSnapshot,
      sessionID: "new-application-test",
    })
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 140, height: 32, useMouse: true, enableMouseMovement: true },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const rows = setup.captureCharFrame().split("\n")
    const y = rows.findIndex((row) => row.includes("+ New Application"))
    const x = y < 0 ? -1 : rows[y]!.indexOf("+ New Application")
    expect({ x, y }).not.toEqual({ x: -1, y: -1 })
    await setup.mockMouse.click(x + 2, y)
    await Bun.sleep(20)
    await setup.renderOnce()

    expect(session.state.studioTarget).toEqual({ type: "new" })
    expect(session.state.messages.at(-1)?.content).toContain("目标、输入、输出和验收标准")
    expect(setup.captureCharFrame()).toContain("New Application · Application Only")
  })

  test("wide terminals keep Studio chat and the Application directory together", async () => {
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
    expect(frame).toContain("AgentLoom Application Studio")
    expect(frame).toContain("创建 · 修改 · 验证 · 运行")
    expect(frame).toContain("Models: powerful* · fast")
    expect(frame).toContain("Application Studio")
    expect(frame).toContain("0 Applications · 0 Global Skills")
    expect(frame).toContain("+ New Application")
    expect(frame).toContain("Ctrl+X")
    expect(frame).toContain("/help")
  })

  test("Workspace does not present Application-local Skills as Global Skills", async () => {
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
    expect(frame).toContain("1 Application · 0 Global Skills")
    expect(frame).not.toContain("已发现 1 个 Skill")

    setup.mockInput.pressKey("x", { ctrl: true })
    await Bun.sleep(10)
    await setup.mockInput.typeText("helper-skill")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).toContain("helper-skill")
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
    expect(setup.captureCharFrame()).toContain("AgentLoom Application Studio")
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

    setup.mockInput.pressKey("x", { ctrl: true })
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
    expect(setup.captureCharFrame()).toContain("AgentLoom Application Studio")
    expect(setup.captureCharFrame()).toContain("Agent")
  })

  test("catalog palette opens main Agents while Worker Agents stay inside their detail", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method) {
        if (method === "application.detail") {
          const capability = { value: {}, source: "global", source_path: "config/system.yaml" }
          return {
            schema_version: 1,
            application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
            working_revision: "sha256:catalog",
            running_revision: null,
            agents: [{
              id: "applications/new/workflows/new.yaml",
              name: "new_agent",
              description: "not run",
              role: "supervisor",
              workflow: "coordinate",
              model: { type: "powerful", source: "global" },
              tools: [],
              skills: [],
              permissions: capability,
              hooks: capability,
              mcp: capability,
              source_path: "applications/new/workflows/new.yaml",
              validation: { valid: true, errors: [] },
              workers: [{
                id: "applications/new/workflows/worker_agents/helper.yaml",
                name: "helper",
                description: "assist the supervisor",
                role: "worker",
                workflow: "help",
                model: { type: "powerful", source: "global" },
                tools: [],
                skills: [],
                permissions: capability,
                hooks: capability,
                mcp: capability,
                source_path: "applications/new/workflows/worker_agents/helper.yaml",
                validation: { valid: true, errors: [] },
                workers: [],
              }],
            }],
          } as RpcResult<Method>
        }
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
      setup.mockInput.pressKey("x", { ctrl: true })
      await Bun.sleep(5)
      await setup.mockInput.typeText(query)
      setup.mockInput.pressEnter()
      await Bun.sleep(20)
      await setup.renderOnce()
      return setup.captureCharFrame()
    }

    const supervisorFrame = await open("new_agent")
    expect(session.state.route).toEqual({
      type: "agent",
      agentID: "applications/new/workflows/new.yaml",
      systemID: "applications/new/workflows/new.yaml",
    })
    expect(session.state.applicationDetail?.agents[0]?.id).toBe("applications/new/workflows/new.yaml")
    expect(supervisorFrame).toContain("Supervisor Agent")
    expect(setup.captureCharFrame()).toContain("Effective Config")
    setup.mockInput.pressEscape()
    await Bun.sleep(50)
    await setup.renderOnce()

    setup.mockInput.pressKey("x", { ctrl: true })
    await Bun.sleep(5)
    await setup.mockInput.typeText("helper")
    await setup.renderOnce()
    expect(setup.captureCharFrame()).not.toContain("Worker Agent")
    setup.mockInput.pressEscape()
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
    expect(setup.captureCharFrame()).toContain("Worker · helper")
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

    await setup.mockMouse.click(120, 8)
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

  test("PageUp scrolls long Studio help in a narrow terminal", async () => {
    const client: TuiClient = {
      async request<Method extends RpcMethod>() {
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "chat-scroll-test" })
    await session.submit("/help")
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 80, height: 24 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const chat = setup.renderer.root.findDescendantById(
      "agentloom-chat-scrollbox",
    ) as ScrollBoxRenderable
    expect(chat.scrollHeight).toBeGreaterThan(chat.height)
    expect(chat.scrollTop).toBeGreaterThan(0)
    expect(setup.captureCharFrame()).not.toContain("Application Studio 帮助")

    let frame = ""
    for (let index = 0; index < 8; index += 1) {
      setup.mockInput.pressKey(PAGE_UP_KEY)
      await setup.renderOnce()
      frame = setup.captureCharFrame()
      if (frame.includes("Application Studio 帮助")) break
    }

    expect(chat.scrollTop).toBeLessThan(chat.scrollHeight - chat.height)
    expect(frame).toContain("Application Studio 帮助")
  })

  test("submitting after reading old chat returns to the newest turn", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "assistant.send") {
          messages.push(String((params as RpcParams<"assistant.send">).message))
          return {
            session_id: "chat-submit-scroll-test",
            assistant: "LATEST_RESPONSE_VISIBLE",
            model_type: "powerful",
            draft: { revision: 0, valid: false, errors: [], files: [] },
          } as RpcResult<Method>
        }
        return snapshot as RpcResult<Method>
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "chat-submit-scroll-test" })
    await session.submit("/help")
    const setup = await testRender(
      () => <AgentLoomApp session={session} projectRoot="/repo" onExit={() => {}} refreshIntervalMs={0} />,
      { width: 80, height: 24 },
    )
    renderers.push(setup.renderer)
    await setup.renderOnce()

    const chat = setup.renderer.root.findDescendantById(
      "agentloom-chat-scrollbox",
    ) as ScrollBoxRenderable
    chat.scrollTo(0)
    await setup.renderOnce()
    expect(chat.scrollTop).toBe(0)

    await setup.mockInput.typeText("newest turn")
    setup.mockInput.pressEnter()
    await Bun.sleep(100)
    await setup.renderOnce()

    expect(messages).toEqual(["newest turn"])
    expect(chat.scrollTop).toBeGreaterThan(0)
    expect(setup.captureCharFrame()).toContain("LATEST_RESPONSE_VISIBLE")
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

    expect(setup.captureCharFrame()).toContain("Studio 模型 · OpenCode Providers")
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

    setup.mockInput.pressKey("x", { ctrl: true })
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

    setup.mockInput.pressKey("x", { ctrl: true })
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
