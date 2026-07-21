import { describe, expect, test } from "bun:test"
import type {
  BootstrapResultDto,
  RpcMethod,
  RpcParams,
  RpcResult,
  RunDetailResultDto,
  SystemDetailResultDto,
} from "../../src/domain"
import { AgentLoomSession, type StudioClient, type TuiClient } from "../../src/app/session"
import { buildPaletteItems } from "../../src/app/controller"
import type { UpdateClient } from "../../src/update/source-updater"

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

class FakeClient implements TuiClient {
  readonly calls: Array<{ method: string; params: Record<string, unknown> }> = []
  responses = new Map<string, unknown>()

  async request<Method extends RpcMethod>(
    method: Method,
    params: RpcParams<Method>,
  ): Promise<RpcResult<Method>> {
    this.calls.push({ method, params: params as unknown as Record<string, unknown> })
    const response = this.responses.get(method)
    if (response instanceof Error) throw response
    return response as RpcResult<Method>
  }

  close() {}
}

type Deferred<Value> = {
  promise: Promise<Value>
  resolve: (value: Value) => void
  reject: (reason: Error) => void
}

function deferred<Value>(): Deferred<Value> {
  let resolve!: (value: Value) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<Value>((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

function systemDetailWithDescription(description: string): SystemDetailResultDto {
  return {
    ...systemDetail,
    summary: { ...systemDetail.summary, description },
    definition: { ...systemDetail.definition, description },
  }
}

function deferredSystemDetailClient(requests: Deferred<SystemDetailResultDto>[]): TuiClient {
  return {
    request<Method extends RpcMethod>(method: Method) {
      if (method !== "system.detail") throw new Error(`unexpected method: ${method}`)
      const request = deferred<SystemDetailResultDto>()
      requests.push(request)
      return request.promise as Promise<RpcResult<Method>>
    },
    close() {},
  }
}

describe("AgentLoom TUI session", () => {
  test("checks for source updates in the background and installs only after user action", async () => {
    const actions: string[] = []
    const updater: UpdateClient = {
      async check() {
        actions.push("check")
        return { available: true, sourceRoot: "/trusted/source", installedAt: 1_000, latestSourceMtime: 2_000 }
      },
      async install() { actions.push("install") },
    }
    const session = new AgentLoomSession({
      client: new FakeClient(),
      snapshot: catalogSnapshot,
      updater,
    })

    await session.start()
    await Bun.sleep(0)
    expect(session.state.updatePhase).toBe("available")

    expect(await session.installUpdate()).toBe(true)
    expect(actions).toEqual(["check", "install"])
    expect(session.state.updatePhase).toBe("installed")
  })

  test("opening an Application loads its effective domain detail", async () => {
    const client = new FakeClient()
    client.responses.set("application.detail", {
      schema_version: 1,
      application: {
        id: "new",
        name: "new",
        path: "applications/new",
        health: "healthy",
        updated_at: "2026-07-20T10:00:00Z",
      },
      working_revision: "sha256:working",
      running_revision: null,
      agents: [],
    })
    const session = new AgentLoomSession({ client, snapshot: catalogSnapshot, sessionID: "application-detail" })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")

    await session.openEntry(application.entry)

    expect(client.calls).toContainEqual({
      method: "application.detail",
      params: { application_id: "new" },
    })
    expect((session.state as unknown as { applicationDetail: { working_revision: string } }).applicationDetail)
      .toMatchObject({ working_revision: "sha256:working" })
  })

  test("selected Applications chat through their persistent OpenCode session", async () => {
    const domain = new FakeClient()
    const studioCalls: string[] = []
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication(applicationID) {
        studioCalls.push(`open:${applicationID}`)
        return {
          sessionID: `opencode-${applicationID}`,
          messages: [{ id: 1, role: "assistant", content: `resumed ${applicationID}` }],
        }
      },
      async send(sessionID, message) {
        studioCalls.push(`send:${sessionID}:${message}`)
        return {
          messages: [
            { id: 1, role: "assistant", content: "resumed new" },
            { id: 2, role: "user", content: message },
            { id: 3, role: "assistant", content: "configuration updated" },
          ],
        }
      },
    }
    const session = new AgentLoomSession({
      client: domain,
      studio,
      snapshot: catalogSnapshot,
      sessionID: "studio-session-test",
    })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")

    await session.openEntry(application.entry)
    await session.submit("add a reviewer Worker")

    expect(session.state.studioSessionID).toBe("opencode-new")
    expect(session.state.messages.at(-1)?.content).toBe("configuration updated")
    expect(studioCalls).toEqual([
      "open:new",
      "send:opencode-new:add a reviewer Worker",
    ])
    expect(domain.calls.some((call) => call.method === "builder.send")).toBe(false)
  })

  test("switching Applications keeps the active Studio memory until /new", async () => {
    const twoApplications: BootstrapResultDto = {
      ...catalogSnapshot,
      applications: [
        catalogSnapshot.applications[0]!,
        {
          ...catalogSnapshot.applications[0]!,
          id: "second",
          name: "second",
          path: "applications/second",
        },
      ],
    }
    const calls: string[] = []
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication(applicationID, permissionMode) {
        calls.push(`open:${applicationID}:${permissionMode}`)
        return {
          sessionID: "ses_shared",
          messages: [{ id: "memory", role: "assistant", content: "已有记忆" }],
        }
      },
      async switchTarget(sessionID, target, permissionMode) {
        calls.push(`switch:${sessionID}:${target.type === "new" ? "new" : target.applicationID}:${permissionMode}`)
      },
      async newSession(target, permissionMode) {
        calls.push(`new:${target.type === "new" ? "new" : target.applicationID}:${permissionMode}`)
        return { sessionID: "ses_fresh", messages: [] }
      },
      async send() { return { messages: [] } },
      async setPermissionMode(sessionID, mode) { calls.push(`permission:${sessionID}:${mode}`) },
    }
    const client = new FakeClient()
    client.responses.set("application.detail", {
      schema_version: 1,
      application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
      working_revision: "sha256:test",
      running_revision: null,
      agents: [],
    })
    const session = new AgentLoomSession({ client, studio, snapshot: twoApplications })
    const applications = buildPaletteItems(twoApplications)
      .filter((item) => item.category === "Applications" && "entry" in item)
    const first = applications.find((item) => "entry" in item && item.entry.kind === "application" && item.entry.applicationID === "new")
    const second = applications.find((item) => "entry" in item && item.entry.kind === "application" && item.entry.applicationID === "second")
    if (!first || !("entry" in first) || !second || !("entry" in second)) throw new Error("missing Applications")

    await session.setPermissionMode("full_access")
    expect(session.state.permissionMode).toBe("full_access")
    await session.openEntry(first.entry)
    await session.openEntry(second.entry)

    expect(session.state.studioSessionID).toBe("ses_shared")
    expect(session.state.messages).toEqual([{ id: "memory", role: "assistant", content: "已有记忆" }])
    await session.submit("/new")

    expect(session.state.studioSessionID).toBe("ses_fresh")
    expect(calls).toEqual([
      "open:new:full_access",
      "switch:ses_shared:second:full_access",
      "new:second:full_access",
    ])

    await session.togglePermissionMode()
    expect(session.state.permissionMode).toBe("application_only")
    expect(calls.at(-1)).toBe("permission:ses_fresh:application_only")
  })

  test("does not retarget the Studio Session while its Agent Loop is active", async () => {
    const twoApplications: BootstrapResultDto = {
      ...catalogSnapshot,
      applications: [
        catalogSnapshot.applications[0]!,
        { ...catalogSnapshot.applications[0]!, id: "second", name: "second", path: "applications/second" },
      ],
    }
    const turn = deferred<{ messages: Array<{ id: string; role: "assistant"; content: string }> }>()
    const switches: string[] = []
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_busy", messages: [] } },
      async switchTarget(_sessionID, target) {
        switches.push(target.type === "new" ? "new" : target.applicationID)
      },
      send() { return turn.promise },
    }
    const client = new FakeClient()
    client.responses.set("application.detail", {
      schema_version: 1,
      application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
      working_revision: "sha256:test",
      running_revision: null,
      agents: [],
    })
    const session = new AgentLoomSession({ client, studio, snapshot: twoApplications })
    const applications = buildPaletteItems(twoApplications)
      .filter((item) => item.category === "Applications" && "entry" in item)
    const first = applications.find((item) => "entry" in item && item.entry.kind === "application" && item.entry.applicationID === "new")
    const second = applications.find((item) => "entry" in item && item.entry.kind === "application" && item.entry.applicationID === "second")
    if (!first || !("entry" in first) || !second || !("entry" in second)) throw new Error("missing Applications")
    await session.openEntry(first.entry)

    const running = session.submit("run a long task")
    await Bun.sleep(0)
    await session.openEntry(first.entry)
    await session.openEntry(second.entry)

    expect(session.state.studioTarget).toEqual({ type: "application", applicationID: "new" })
    expect(switches).toEqual([])
    expect(session.state.notice).toContain("Agent Loop")
    turn.resolve({ messages: [{ id: "done", role: "assistant", content: "done" }] })
    await running
  })

  test("keeps completed subagent text visible for selection and copy", async () => {
    let listener: ((event: import("../../src/app/session").StudioEvent) => void) | undefined
    const turn = deferred<{ messages: Array<{ id: string; role: "assistant"; content: string }> }>()
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_trace", messages: [] } },
      send() { return turn.promise },
      subscribe(next) {
        listener = next
        return () => { listener = undefined }
      },
    }
    const session = new AgentLoomSession({ client: new FakeClient(), studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)

    const running = session.submit("delegate")
    await Bun.sleep(0)
    listener?.({
      type: "text.delta",
      sessionID: "ses_trace",
      text: "inspected worker output",
      source: { kind: "subagent", sessionID: "ses_child" },
    })
    turn.resolve({ messages: [{ id: "done", role: "assistant", content: "final" }] })
    await running

    expect(session.state.streamingText).toContain("inspected worker output")
    expect(session.state.loopState).toBe("idle")
  })

  test("an interrupted Studio turn cannot overwrite the immediate follow-up", async () => {
    const first = deferred<{ messages: Array<{ id: string; role: "assistant"; content: string }> }>()
    const second = deferred<{ messages: Array<{ id: string; role: "assistant"; content: string }> }>()
    let sendCount = 0
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_interrupt", messages: [] } },
      send() {
        sendCount += 1
        return sendCount === 1 ? first.promise : second.promise
      },
      async interrupt() {},
    }
    const session = new AgentLoomSession({ client: new FakeClient(), studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)

    const interrupted = session.submit("inspect everything")
    await Bun.sleep(0)
    await session.interruptStudio()
    const followUp = session.submit("只回答：中止恢复成功")
    second.resolve({ messages: [{ id: "new", role: "assistant", content: "中止恢复成功" }] })
    await followUp
    first.resolve({ messages: [{ id: "stale", role: "assistant", content: "OLD TURN" }] })
    await interrupted

    expect(session.state.messages).toEqual([{ id: "new", role: "assistant", content: "中止恢复成功" }])
    expect(session.state.assistantBusy).toBe(false)
    expect(session.state.notice ?? "").not.toContain("OLD TURN")
  })

  test("New Application binds a fresh OpenCode session before accepting its first request", async () => {
    const calls: string[] = []
    const studio: StudioClient = {
      async openNewApplication() {
        calls.push("open-new")
        return { sessionID: "ses_create", messages: [] }
      },
      async openApplication() { throw new Error("not expected") },
      async send(sessionID, message) {
        calls.push(`send:${sessionID}:${message}`)
        return { messages: [{ id: "created", role: "assistant", content: "created" }] }
      },
    }
    const session = new AgentLoomSession({ client: new FakeClient(), studio, snapshot: catalogSnapshot })

    await session.beginApplicationCreation()
    await session.submit("创建一个内容审核 Application")

    expect(session.state.studioTarget).toEqual({ type: "new" })
    expect(session.state.studioSessionID).toBe("ses_create")
    expect(calls).toEqual([
      "open-new",
      "send:ses_create:创建一个内容审核 Application",
    ])
  })

  test("surfaces OpenCode permission requests and sends once/always/reject replies", async () => {
    const domain = new FakeClient()
    domain.responses.set("application.detail", {
      schema_version: 1,
      application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
      working_revision: "sha256:one",
      running_revision: null,
      agents: [],
    })
    let listener: ((event: import("../../src/app/session").StudioEvent) => void) | undefined
    const replies: string[] = []
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_new", messages: [] } },
      async send(_sessionID, message) {
        return { messages: [{ id: "done", role: "assistant", content: `done ${message}` }] }
      },
      subscribe(next) {
        listener = next
        return () => { listener = undefined }
      },
      async replyPermission(requestID, reply) { replies.push(`${requestID}:${reply}`) },
    }
    const session = new AgentLoomSession({ client: domain, studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)

    listener?.({
      type: "permission",
      sessionID: "ses_new",
      requestID: "per_run",
      permission: "bash",
      patterns: ["loom run *"],
      metadata: { command: "loom run applications/new/workflows/new.yaml" },
    })

    expect((session.state as any).loopState).toBe("waiting_permission")
    expect((session.state as any).permissionRequest).toMatchObject({ requestID: "per_run", permission: "bash" })
    await (session as any).respondPermission("always")
    expect(replies).toEqual(["per_run:always"])
    expect((session.state as any).permissionRequest).toBeNull()
  })

  test("surfaces OpenCode questions and sends one answer per question", async () => {
    const domain = new FakeClient()
    domain.responses.set("application.detail", {
      schema_version: 1,
      application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
      working_revision: "sha256:one",
      running_revision: null,
      agents: [],
    })
    let listener: ((event: import("../../src/app/session").StudioEvent) => void) | undefined
    const replies: Array<{ requestID: string; answers?: string[][] }> = []
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_question", messages: [] } },
      async send() { return { messages: [] } },
      subscribe(next) {
        listener = next
        return () => { listener = undefined }
      },
      async replyQuestion(requestID, answers) { replies.push({ requestID, answers }) },
      async rejectQuestion(requestID) { replies.push({ requestID }) },
    }
    const session = new AgentLoomSession({ client: domain, studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)

    listener?.({
      type: "question",
      sessionID: "ses_question",
      requestID: "que_design",
      questions: [
        { header: "Topology", question: "How many Workers?", options: [], multiple: false, custom: true },
        { header: "Output", question: "Which format?", options: [], multiple: false, custom: true },
      ],
    })

    expect(session.state.questionRequest?.requestID).toBe("que_design")
    await session.respondQuestion("3 | JSON")
    expect(replies).toEqual([{ requestID: "que_design", answers: [["3"], ["JSON"]] }])
    expect(session.state.questionRequest).toBeNull()

    listener?.({
      type: "question",
      sessionID: "ses_question",
      requestID: "que_reject",
      questions: [{ header: "Scope", question: "Expand scope?", options: [], multiple: false, custom: true }],
    })
    await session.rejectQuestion()
    expect(replies.at(-1)).toEqual({ requestID: "que_reject" })
  })

  test("switches Full Access only on the currently bound Studio session", async () => {
    const modes: string[] = []
    const studio: StudioClient = {
      async openNewApplication() { throw new Error("not expected") },
      async openApplication() { return { sessionID: "ses_scope", messages: [] } },
      async send() { return { messages: [] } },
      async setPermissionMode(sessionID, mode) { modes.push(`${sessionID}:${mode}`) },
    }
    const session = new AgentLoomSession({ client: new FakeClient(), studio, snapshot: catalogSnapshot })
    const application = buildPaletteItems(catalogSnapshot).find((item) => item.category === "Applications")
    if (!application || !("entry" in application)) throw new Error("missing Application")
    await session.openEntry(application.entry)

    await session.setPermissionMode("full_access")

    expect(session.state.permissionMode).toBe("full_access")
    expect(modes).toEqual(["ses_scope:full_access"])
  })

  test("loads effective detail for Applications and main Agents while Workers stay out of the catalog", async () => {
    const client = new FakeClient()
    client.responses.set("application.detail", {
      schema_version: 1,
      application: { id: "new", name: "new", path: "applications/new", health: "healthy", updated_at: null },
      working_revision: "sha256:catalog",
      running_revision: null,
      agents: [],
    })
    const session = new AgentLoomSession({ client, snapshot: catalogSnapshot, sessionID: "catalog-1" })
    const items = buildPaletteItems(catalogSnapshot)
    const byCategory = (category: string) => {
      const item = items.find((candidate) => candidate.category === category)
      if (!item || !("entry" in item)) throw new Error(`missing ${category}`)
      return item.entry
    }
    const byTitle = (title: string) => {
      const item = items.find((candidate) => candidate.title === title)
      if (!item || !("entry" in item)) throw new Error(`missing ${title}`)
      return item.entry
    }

    await session.openEntry(byCategory("Applications"))
    expect(session.state.route).toEqual({ type: "application", applicationID: "new" })
    expect(items.find((item) => item.title === "helper")).toBeUndefined()
    await session.openEntry(byTitle("new_agent"))
    expect(session.state.route).toEqual({
      type: "agent",
      agentID: "applications/new/workflows/new.yaml",
      systemID: "applications/new/workflows/new.yaml",
    })
    expect(session.state.applicationDetail?.application.id).toBe("new")
    await session.openEntry(byCategory("Skills"))
    expect(session.state.route).toEqual({ type: "skill", skillID: "new:helper-skill" })
    await session.openEntry(byCategory("Schedules"))
    expect(session.state.route).toEqual({ type: "schedule", scheduleID: "new-hourly" })
    expect(client.calls).toEqual([
      {
        method: "application.detail",
        params: { application_id: "new" },
      },
      {
        method: "application.detail",
        params: { application_id: "new" },
      },
    ])
    expect(session.state.detailBusy).toBe(false)
  })

  test("renders a chat-ready workspace shell before the catalog bootstrap finishes", async () => {
    const bootstrap = deferred<BootstrapResultDto>()
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method) {
        if (method !== "bootstrap") throw new Error(`unexpected method: ${method}`)
        return bootstrap.promise as Promise<RpcResult<Method>>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      projectRoot: "/repo",
      sessionID: "chat-1",
    })

    const loading = session.start()

    expect(session.state.workspacePhase).toBe("loading")
    expect(session.state.runsIncomplete).toBe(false)
    expect(session.state.snapshot.project).toEqual({ root: "/repo", name: "repo" })
    expect(session.state.messages[0]?.content).toContain("新建或选择一个 Application")
    expect(session.state.busy).toBe(false)

    bootstrap.resolve(snapshot)
    await loading

    expect(session.state.workspacePhase).toBe("ready")
    expect(session.state.runsIncomplete).toBe(false)
    expect(session.state.snapshot.systems).toHaveLength(1)
    expect(session.state.modelType).toBe("powerful")
  })

  test("shows the sanitized configured model catalog without an RPC", async () => {
    const client = new FakeClient()
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("/models")

    expect(client.calls).toEqual([])
    expect(session.state.messages.at(-1)?.content).toContain("powerful (默认) — Best quality")
    expect(session.state.messages.at(-1)?.content).toContain("fast — Lower latency")
    expect(session.state.messages.at(-1)?.content).not.toContain("api_key")
  })

  test("shows deterministic Studio help without asking the model", async () => {
    const client = new FakeClient()
    const session = new AgentLoomSession({ client, snapshot: catalogSnapshot })

    await session.submit("/help")

    const help = session.state.messages.at(-1)?.content ?? ""
    expect(help).toContain("Ctrl+X")
    expect(help).toContain("Application Only")
    expect(help).toContain("/models")
    expect(help).not.toContain("F6")
    expect(help).not.toContain("/apply")
    expect(client.calls).toEqual([])
  })

  test("manages durable schedules through explicit local commands", async () => {
    const client = new FakeClient()
    client.responses.set("bootstrap", catalogSnapshot)
    client.responses.set("schedule.add", {
      action: "add",
      job_id: "job_new",
      name: "hourly new",
      state: "scheduled",
    })
    const stoppedScheduleSnapshot: BootstrapResultDto = {
      ...catalogSnapshot,
      schedules: {
        ...catalogSnapshot.schedules,
        service: { ...catalogSnapshot.schedules.service, state: "stopped" },
      },
    }
    const session = new AgentLoomSession({
      client,
      snapshot: stoppedScheduleSnapshot,
      sessionID: "schedule-1",
    })

    await session.submit("/schedule")
    expect(session.state.messages.at(-1)?.content).toContain("/schedule add <agent.yaml>")
    expect(session.state.messages.at(-1)?.content).toContain(
      "agentloom schedules --project '/repo' serve",
    )
    expect(client.calls).toEqual([])

    await session.submit(
      "/schedule add applications/new/workflows/new.yaml --every 1h --name \"hourly new\"",
    )
    expect(client.calls.slice(0, 2)).toEqual([
      {
        method: "schedule.add",
        params: {
          yaml_path: "applications/new/workflows/new.yaml",
          name: "hourly new",
          schedule: { kind: "interval", every: "1h", timezone: "UTC" },
        },
      },
      { method: "bootstrap", params: {} },
    ])
    expect(session.state.messages.at(-1)?.content).toContain("Schedule add: hourly new (job_new)")
    expect(session.state.messages.at(-1)?.content).toContain("调度服务当前未运行")

    client.responses.set("schedule.pause", {
      action: "pause",
      job_id: "job_new",
      name: "hourly new",
      state: "paused",
    })
    await session.submit("/schedule pause job_new")
    expect(client.calls.at(-2)).toEqual({ method: "schedule.pause", params: { job_id: "job_new" } })
    expect(session.state.messages.at(-1)?.content).toContain("paused")

    const callCount = client.calls.length
    await session.submit("/schedule add ../outside.yaml --every 1h")
    expect(client.calls).toHaveLength(callCount)
    expect(session.state.notice).toContain("不在当前项目目录")
  })

  test("refreshes live state without losing the selected detail route", async () => {
    const client = new FakeClient()
    client.responses.set("system.detail", systemDetail)
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.openEntry(session.entries[0]!)
    client.responses.set("bootstrap", {
      ...snapshot,
      systems: [{ ...snapshot.systems[0], description: "updated" }],
    })
    await session.refresh()

    expect(session.state.route).toEqual({
      type: "system",
      systemID: "applications/new/workflows/new.yaml",
    })
    expect(session.state.snapshot.systems[0]!.description).toBe("updated")
    expect(client.calls.map((call) => call.method)).toEqual([
      "system.detail",
      "bootstrap",
      "system.detail",
    ])
  })

  test("keeps model selection local and sends chat through the bounded builder RPC", async () => {
    const client = new FakeClient()
    client.responses.set("assistant.send", {
      session_id: "builder-1",
      assistant: "已生成草稿，请确认后 /apply。",
      model_type: "fast",
      draft: {
        revision: 3,
        valid: true,
        errors: [],
        files: [
          {
            path: "applications/new/workflows/new.yaml",
            change: "create",
            content: "name: new_agent",
          },
        ],
      },
    })
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("/model fast")
    await session.submit("创建一个总结 Agent")

    expect(session.state.modelType).toBe("fast")
    expect(client.calls).toEqual([
      {
        method: "assistant.send",
        params: { session_id: "builder-1", message: "创建一个总结 Agent", model_type: "fast" },
      },
    ])
    expect(session.state.messages.at(-1)?.content).toBe("已生成草稿，请确认后 /apply。")
    expect(session.state.draft?.revision).toBe(3)
  })

  test("accepts a null model from the builder without discarding the user's selected model", async () => {
    const client = new FakeClient()
    client.responses.set("assistant.send", {
      session_id: "builder-1",
      assistant: "draft ready",
      model_type: null,
      draft: { revision: 1, valid: true, errors: [], files: [] },
    })
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("/model fast")
    await session.submit("prepare")

    expect(session.state.modelType).toBe("fast")
  })

  test("recovers the latest backend draft when Builder fails after staging", async () => {
    const client = new FakeClient()
    client.responses.set("assistant.send", new Error("provider disconnected after staging"))
    client.responses.set("builder.draft", {
      revision: 2,
      valid: true,
      errors: [],
      files: [
        {
          path: "applications/new/workflows/new.yaml",
          change: "create",
          content: "name: recovered",
        },
      ],
    })
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("prepare")

    expect(client.calls.map((call) => call.method)).toEqual(["assistant.send", "builder.draft"])
    expect(session.state.notice).toBe("provider disconnected after staging")
    expect(session.state.draft?.revision).toBe(2)
    expect(session.state.draft?.files[0]?.content).toBe("name: recovered")
  })

  test("keeps a recovered Builder error visible across live refreshes", async () => {
    const client = new FakeClient()
    client.responses.set("assistant.send", new Error("provider disconnected after staging"))
    client.responses.set("builder.draft", {
      revision: 2,
      valid: true,
      errors: [],
      files: [],
    })
    client.responses.set("bootstrap", snapshot)
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("prepare")
    await session.refresh()

    expect(session.state.notice).toBe("provider disconnected after staging")
  })

  test("applies only the exact draft revision and refreshes the directory", async () => {
    const client = new FakeClient()
    client.responses.set("assistant.send", {
      session_id: "builder-1",
      assistant: "draft ready",
      model_type: "powerful",
      draft: { revision: 7, valid: true, errors: [], files: [] },
    })
    client.responses.set("draft.apply", { applied: true, revision: 7, files: [] })
    client.responses.set("bootstrap", snapshot)
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("prepare")
    await session.submit("/apply")

    expect(client.calls.at(1)).toEqual({
      method: "draft.apply",
      params: { session_id: "builder-1", expected_revision: 7 },
    })
    expect(client.calls.at(2)?.method).toBe("bootstrap")
    expect(session.state.messages.at(-1)?.content).toContain("revision 7")
  })

  test("refreshes the backend draft after an apply conflict", async () => {
    const client = new FakeClient()
    client.responses.set("assistant.send", {
      session_id: "builder-1",
      assistant: "draft ready",
      model_type: "powerful",
      draft: { revision: 1, valid: true, errors: [], files: [] },
    })
    const conflict = new Error("Draft revision conflict: expected 1, current 2") as Error & { code: string }
    conflict.code = "draft_conflict"
    client.responses.set("draft.apply", conflict)
    client.responses.set("builder.draft", {
      revision: 2,
      valid: true,
      errors: [],
      files: [
        {
          path: "applications/new/workflows/new.yaml",
          change: "create",
          content: "name: current",
        },
      ],
    })
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("prepare")
    await session.submit("/apply")

    expect(client.calls.map((call) => call.method)).toEqual([
      "assistant.send",
      "draft.apply",
      "builder.draft",
    ])
    expect(session.state.notice).toBe("Draft revision conflict: expected 1, current 2")
    expect(session.state.draft?.revision).toBe(2)
  })

  test("never sends /apply when no validated draft exists", async () => {
    const client = new FakeClient()
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.submit("/apply")

    expect(client.calls).toEqual([])
    expect(session.state.notice).toBe("当前没有可应用的有效草稿")
  })

  test("ignores an older successful detail response for the same route", async () => {
    const requests: Deferred<SystemDetailResultDto>[] = []
    const session = new AgentLoomSession({
      client: deferredSystemDetailClient(requests),
      snapshot,
      sessionID: "builder-1",
    })
    const entry = session.entries[0]!

    const older = session.openEntry(entry)
    const latest = session.openEntry(entry)
    requests[1]!.resolve(systemDetailWithDescription("latest"))
    await latest
    requests[0]!.resolve(systemDetailWithDescription("older"))
    await older

    expect(session.state.systemDetail?.summary.description).toBe("latest")
  })

  test("ignores an older detail error for the same route", async () => {
    const requests: Deferred<SystemDetailResultDto>[] = []
    const session = new AgentLoomSession({
      client: deferredSystemDetailClient(requests),
      snapshot,
      sessionID: "builder-1",
    })
    const entry = session.entries[0]!

    const older = session.openEntry(entry)
    const latest = session.openEntry(entry)
    requests[1]!.resolve(systemDetailWithDescription("latest"))
    await latest
    requests[0]!.reject(new Error("stale failure"))
    await older

    expect(session.state.notice).toBeNull()
    expect(session.state.systemDetail?.summary.description).toBe("latest")
  })

  test("keeps busy true while the latest request for the same route is pending", async () => {
    const requests: Deferred<SystemDetailResultDto>[] = []
    const session = new AgentLoomSession({
      client: deferredSystemDetailClient(requests),
      snapshot,
      sessionID: "builder-1",
    })
    const entry = session.entries[0]!

    const older = session.openEntry(entry)
    const latest = session.openEntry(entry)
    requests[0]!.resolve(systemDetailWithDescription("older"))
    await older

    expect(session.state.busy).toBe(true)

    requests[1]!.resolve(systemDetailWithDescription("latest"))
    await latest
    expect(session.state.busy).toBe(false)
  })

  test("returning to Builder cancels a pending detail load immediately", async () => {
    const requests: Deferred<SystemDetailResultDto>[] = []
    const session = new AgentLoomSession({
      client: deferredSystemDetailClient(requests),
      snapshot,
      sessionID: "builder-1",
    })

    const opening = session.openEntry(session.entries[0]!)
    expect(session.state.busy).toBe(true)

    session.goBuilder()

    expect(session.state.route).toEqual({ type: "builder" })
    expect(session.state.busy).toBe(false)
    requests[0]!.resolve(systemDetailWithDescription("stale"))
    await opening
    expect(session.state.route).toEqual({ type: "builder" })
    expect(session.state.systemDetail).toBeNull()
  })

  test("a fast detail response cannot clear an active Builder request", async () => {
    const builder = deferred<RpcResult<"assistant.send">>()
    const calls: string[] = []
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method) {
        calls.push(method)
        if (method === "assistant.send") {
          return builder.promise as Promise<RpcResult<Method>>
        }
        if (method === "system.detail") {
          return Promise.resolve(systemDetail) as Promise<RpcResult<Method>>
        }
        throw new Error(`unexpected method: ${method}`)
      },
      close() {},
    }
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    const sending = session.submit("prepare")
    await Promise.resolve()
    await session.openEntry(session.entries[0]!)

    expect(session.state.busy).toBe(true)
    session.goBuilder()
    await session.submit("must not overlap")
    expect(calls.filter((method) => method === "assistant.send")).toHaveLength(1)

    builder.resolve({
      session_id: "builder-1",
      assistant: "draft ready",
      model_type: "powerful",
      draft: { revision: 1, valid: true, errors: [], files: [] },
    })
    await sending

    expect(session.state.busy).toBe(false)
  })

  test("live refresh updates every Agent and Schedule before reloading the open Run", async () => {
    const run = {
      run_id: "run-live",
      system_id: snapshot.systems[0]!.id,
      application_id: "new",
      task_id: "task-live",
      agent_name: "report_agent",
      status: "running" as const,
      started_at: "2026-07-17T10:00:00Z",
      ended_at: null,
    }
    const secondSystem = {
      ...snapshot.systems[0]!,
      id: "applications/other/workflows/other.yaml",
      path: "applications/other/workflows/other.yaml",
      application_id: "other",
      name: "other_agent",
    }
    const secondRun = {
      ...run,
      run_id: "run-other",
      system_id: secondSystem.id,
      application_id: "other",
      task_id: "task-other",
      agent_name: "other_agent",
    }
    const runDetail: RunDetailResultDto = {
      summary: run,
      error: null,
      workers: [],
      events: [],
      logs: [],
      artifacts: [],
      result_state: "running",
      result: null,
      limits: {
        workers: { truncated: false, returned_count: 0, max_count: 256 },
        events: {
          truncated: false,
          source_incomplete: false,
          returned_count: 0,
          returned_bytes: 0,
          max_count: 256,
          max_bytes: 262_144,
          max_scan_bytes: 1_048_576,
        },
        logs: {
          truncated: false,
          returned_count: 0,
          returned_bytes: 0,
          max_count: 16,
          max_bytes: 131_072,
          max_bytes_per_file: 16_384,
          max_scanned_entries: 4_096,
        },
        artifacts: {
          truncated: false,
          returned_count: 0,
          max_count: 256,
          max_scanned_entries: 4_096,
        },
        result: {
          truncated: false,
          source_incomplete: false,
          returned_bytes: 0,
          max_bytes: 262_144,
        },
      },
    }
    const calls: string[] = []
    let detailCalls = 0
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method) {
        calls.push(method)
        if (method === "runtime.summary") {
          const completedRun = {
            ...run,
            status: "completed" as const,
            ended_at: "2026-07-17T10:01:00Z",
          }
          const failedBackgroundRun = {
            ...secondRun,
            status: "failed" as const,
            ended_at: "2026-07-17T10:02:00Z",
          }
          return Promise.resolve({
            systems: [
              { ...snapshot.systems[0]!, state: "completed" as const, latest_run: completedRun },
              { ...secondSystem, state: "failed" as const, latest_run: failedBackgroundRun },
            ],
            runs: [failedBackgroundRun, completedRun],
            runs_incomplete: false,
            removed_runs: [],
            worker_invocations: [],
            worker_invocations_incomplete: false,
            schedules: {
              ...snapshot.schedules,
              service: { ...snapshot.schedules.service, state: "running" as const },
            },
          }) as Promise<RpcResult<Method>>
        }
        if (method === "run.detail") {
          detailCalls += 1
          const result = detailCalls === 1
            ? runDetail
            : {
                ...runDetail,
                summary: {
                  ...runDetail.summary,
                  status: "completed" as const,
                  ended_at: "2026-07-17T10:01:00Z",
                },
                result_state: "available" as const,
                result: "done",
              }
          return Promise.resolve(result) as Promise<RpcResult<Method>>
        }
        throw new Error(`unexpected method: ${method}`)
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: {
        ...snapshot,
        systems: [
          { ...snapshot.systems[0]!, state: "running", latest_run: run },
          { ...secondSystem, state: "running", latest_run: secondRun },
        ],
        runs: [run, secondRun],
      },
      sessionID: "builder-1",
    })

    const selectedRun = session.entries.find(
      (entry) => entry.kind === "run" && entry.runID === run.run_id,
    )!
    await session.openEntry(selectedRun)
    const selectedIndexBefore = session.state.selectedIndex
    calls.length = 0
    await session.refreshLive()

    expect(calls).toEqual(["runtime.summary", "run.detail"])
    expect(session.state.snapshot.runs.find((item) => item.run_id === "run-live")?.status).toBe("completed")
    expect(session.state.snapshot.runs.find((item) => item.run_id === "run-other")?.status).toBe("failed")
    expect(session.state.snapshot.systems[0]!.state).toBe("completed")
    expect(session.state.snapshot.systems[0]!.latest_run?.status).toBe("completed")
    expect(session.state.snapshot.systems[1]!.state).toBe("failed")
    expect(session.state.snapshot.schedules.service.state).toBe("running")
    expect(session.state.selectedIndex).not.toBe(selectedIndexBefore)
    expect(session.entries[session.state.selectedIndex]!.key).toBe(selectedRun.key)
    expect(session.entries[session.state.selectedIndex]).toMatchObject({
      kind: "run",
      runID: "run-live",
      status: "completed",
    })
  })

  test("keeps the clicked application selected while same-id unlinked runs refresh", async () => {
    const runs = ["alpha", "beta"].map((applicationID) => ({
      run_id: "shared-run",
      system_id: null,
      application_id: applicationID,
      task_id: `task-${applicationID}`,
      agent_name: applicationID,
      status: "running" as const,
      started_at: "2026-07-17T10:00:00Z",
      ended_at: null,
    }))
    let betaDetailCalls = 0
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "runtime.summary") {
          return Promise.resolve({
            systems: [],
            runs: runs.map((run) => run.application_id === "beta"
              ? { ...run, status: "completed" as const, ended_at: "2026-07-17T10:01:00Z" }
              : run),
            runs_incomplete: false,
            removed_runs: [],
            worker_invocations: [],
            worker_invocations_incomplete: false,
            schedules: snapshot.schedules,
          }) as Promise<RpcResult<Method>>
        }
        if (method !== "run.detail") throw new Error(`unexpected method: ${method}`)
        const applicationID = (params as RpcParams<"run.detail">).application_id
        const initial = runs.find((run) => run.application_id === applicationID)!
        if (applicationID === "beta") betaDetailCalls += 1
        const summary = applicationID === "beta" && betaDetailCalls > 1
          ? { ...initial, status: "completed" as const, ended_at: "2026-07-17T10:01:00Z" }
          : initial
        return Promise.resolve({
          summary,
          error: null,
          workers: [],
          events: [],
          logs: [],
          artifacts: [],
          result_state: summary.status,
          result: summary.status === "completed" ? "done" : null,
          limits: {
            workers: { truncated: false, returned_count: 0, max_count: 256 },
            events: {
              truncated: false,
              source_incomplete: false,
              returned_count: 0,
              returned_bytes: 0,
              max_count: 256,
              max_bytes: 262_144,
              max_scan_bytes: 1_048_576,
            },
            logs: {
              truncated: false,
              returned_count: 0,
              returned_bytes: 0,
              max_count: 16,
              max_bytes: 131_072,
              max_bytes_per_file: 16_384,
              max_scanned_entries: 4_096,
            },
            artifacts: {
              truncated: false,
              returned_count: 0,
              max_count: 256,
              max_scanned_entries: 4_096,
            },
            result: {
              truncated: false,
              source_incomplete: false,
              returned_bytes: 0,
              max_bytes: 262_144,
            },
          },
        }) as Promise<RpcResult<Method>>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: { ...snapshot, systems: [], runs },
      sessionID: "builder-1",
    })
    const betaEntry = session.entries.find(
      (entry) => entry.kind === "run" && entry.applicationID === "beta",
    )!

    await session.openEntry(betaEntry)
    expect(session.state.route).toMatchObject({ type: "run", applicationID: "beta" })
    expect(session.entries[session.state.selectedIndex]).toMatchObject({ applicationID: "beta" })

    await session.refreshLive()
    expect(session.entries[session.state.selectedIndex]).toMatchObject({
      applicationID: "beta",
      status: "completed",
    })
  })

  test("refreshes all project Agent states while the Builder remains open", async () => {
    const run = {
      run_id: "background-run",
      system_id: snapshot.systems[0]!.id,
      application_id: "new",
      task_id: "background-task",
      agent_name: "new_agent",
      status: "running" as const,
      started_at: "2026-07-17T10:00:00Z",
      ended_at: null,
    }
    const completed = {
      ...run,
      status: "completed" as const,
      ended_at: "2026-07-17T10:01:00Z",
    }
    const calls: string[] = []
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method) {
        calls.push(method)
        if (method !== "runtime.summary") throw new Error(`unexpected method: ${method}`)
        return Promise.resolve({
          systems: [{ ...snapshot.systems[0]!, state: "completed" as const, latest_run: completed }],
          runs: [completed],
          runs_incomplete: true,
          removed_runs: [],
          worker_invocations: [],
          worker_invocations_incomplete: false,
          schedules: snapshot.schedules,
        }) as Promise<RpcResult<Method>>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: {
        ...snapshot,
        systems: [{ ...snapshot.systems[0]!, state: "running", latest_run: run }],
        runs: [
          run,
          {
            ...run,
            run_id: "historical-run",
            task_id: "historical-task",
            status: "completed",
            started_at: "2026-07-16T10:00:00Z",
            ended_at: "2026-07-16T10:01:00Z",
          },
        ],
        applications: [{
          id: "new",
          name: "new",
          path: "applications/new",
          system_count: 1,
          worker_count: 0,
          skill_count: 0,
          run_count: 1,
          active_run_count: 1,
        }],
      },
      sessionID: "builder-live",
    })

    await session.refreshLive()

    expect(calls).toEqual(["runtime.summary"])
    expect(session.state.route).toEqual({ type: "builder" })
    expect(session.state.snapshot.systems[0]!.state).toBe("completed")
    expect(session.state.snapshot.applications[0]!.active_run_count).toBe(0)
    expect(session.state.snapshot.applications[0]!.run_count).toBe(2)
    expect(session.state.snapshot.runs.map((item) => item.run_id)).toContain("historical-run")
  })

  test("removes a tombstoned run from an incomplete window and closes its detail", async () => {
    const run = {
      run_id: "deleted-run",
      system_id: snapshot.systems[0]!.id,
      application_id: "new",
      task_id: "deleted-task",
      agent_name: "new_agent",
      status: "running" as const,
      started_at: "2026-07-17T10:00:00Z",
      ended_at: null,
    }
    const worker = {
      run_id: run.run_id,
      system_id: run.system_id,
      application_id: run.application_id,
      parent_agent_name: run.agent_name,
      agent_name: "helper",
      call_index: 1,
      status: "running",
      step: 1,
      started_at: run.started_at,
      ended_at: null,
      error: null,
    }
    const detail: RunDetailResultDto = {
      summary: run,
      error: null,
      workers: [],
      events: [],
      logs: [],
      artifacts: [],
      result_state: "running",
      result: null,
      limits: {
        workers: { truncated: false, returned_count: 0, max_count: 256 },
        events: {
          truncated: false,
          source_incomplete: false,
          returned_count: 0,
          returned_bytes: 0,
          max_count: 256,
          max_bytes: 262_144,
          max_scan_bytes: 1_048_576,
        },
        logs: {
          truncated: false,
          returned_count: 0,
          returned_bytes: 0,
          max_count: 16,
          max_bytes: 131_072,
          max_bytes_per_file: 16_384,
          max_scanned_entries: 4_096,
        },
        artifacts: {
          truncated: false,
          returned_count: 0,
          max_count: 256,
          max_scanned_entries: 4_096,
        },
        result: {
          truncated: false,
          source_incomplete: false,
          returned_bytes: 0,
          max_bytes: 262_144,
        },
      },
    }
    const calls: string[] = []
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method) {
        calls.push(method)
        if (method === "bootstrap") {
          return Promise.resolve(snapshot) as Promise<RpcResult<Method>>
        }
        if (method === "run.detail") {
          return Promise.resolve(detail) as Promise<RpcResult<Method>>
        }
        if (method !== "runtime.summary") throw new Error(`unexpected method: ${method}`)
        return Promise.resolve({
          systems: [{ ...snapshot.systems[0]!, state: "never_run" as const, latest_run: null }],
          runs: [],
          runs_incomplete: true,
          removed_runs: [{ application_id: run.application_id, run_id: run.run_id }],
          worker_invocations: [],
          worker_invocations_incomplete: false,
          schedules: snapshot.schedules,
        }) as Promise<RpcResult<Method>>
      },
      close() {},
    }
    const session = new AgentLoomSession({
      client,
      snapshot: {
        ...snapshot,
        systems: [{ ...snapshot.systems[0]!, state: "running", latest_run: run }],
        runs: [run],
        worker_invocations: [worker],
        applications: [{
          id: "new",
          name: "new",
          path: "applications/new",
          system_count: 1,
          worker_count: 1,
          skill_count: 0,
          run_count: 1,
          active_run_count: 1,
        }],
      },
      sessionID: "deleted-run-live",
    })

    const entry = session.entries.find(
      (candidate) => candidate.kind === "run" && candidate.runID === run.run_id,
    )!
    await session.openEntry(entry)
    expect(session.state.runDetail?.summary.run_id).toBe(run.run_id)

    await session.refreshLive()

    expect(calls).toEqual(["run.detail", "runtime.summary"])
    expect(session.state.route).toEqual({ type: "builder" })
    expect(session.state.runDetail).toBeNull()
    expect(session.state.snapshot.runs).toEqual([])
    expect(session.state.snapshot.worker_invocations).toEqual([])
    expect(session.state.snapshot.systems[0]).toMatchObject({ state: "never_run", latest_run: null })
    expect(session.state.snapshot.applications[0]).toMatchObject({
      run_count: 0,
      active_run_count: 0,
    })
    expect(session.entries.some((candidate) => candidate.kind === "run")).toBe(false)
    expect(session.state.runsIncomplete).toBe(true)

    await session.refresh()
    expect(session.state.runsIncomplete).toBe(false)
  })

  test("ignores a stale run detail response when the same run id belongs to another system", async () => {
    type Resolver = (value: RunDetailResultDto) => void
    const resolvers = new Map<string, Resolver>()
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method !== "run.detail") throw new Error(`unexpected method: ${method}`)
        const systemID = (params as RpcParams<"run.detail">).system_id!
        return new Promise<RunDetailResultDto>((resolve) => resolvers.set(systemID, resolve)) as Promise<RpcResult<Method>>
      },
      close() {},
    }
    const runsSnapshot: BootstrapResultDto = {
      ...snapshot,
      systems: [],
      runs: ["system-a", "system-b"].map((systemID) => ({
        run_id: "shared-run",
        system_id: systemID,
        application_id: systemID,
        task_id: `${systemID}-task`,
        agent_name: systemID,
        status: "completed" as const,
        started_at: "2026-07-17T10:00:00Z",
        ended_at: "2026-07-17T10:01:00Z",
      })),
    }
    const session = new AgentLoomSession({ client, snapshot: runsSnapshot, sessionID: "builder-1" })
    const [entryA, entryB] = session.entries
    const detail = (systemID: string): RunDetailResultDto => ({
      summary: runsSnapshot.runs.find((run) => run.system_id === systemID)!,
      error: null,
      workers: [],
      events: [],
      logs: [],
      artifacts: [],
      result_state: "available",
      result: systemID,
      limits: {
        workers: { truncated: false, returned_count: 0, max_count: 256 },
        events: {
          truncated: false,
          source_incomplete: false,
          returned_count: 0,
          returned_bytes: 0,
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
          max_scanned_entries: 2_048,
        },
        artifacts: {
          truncated: false,
          returned_count: 0,
          max_count: 256,
          max_scanned_entries: 2_048,
        },
        result: {
          truncated: false,
          source_incomplete: false,
          returned_bytes: systemID.length,
          max_bytes: 131_072,
        },
      },
    })

    const openingA = session.openEntry(entryA!)
    await Promise.resolve()
    const openingB = session.openEntry(entryB!)
    await Promise.resolve()
    resolvers.get("system-b")!(detail("system-b"))
    await openingB
    resolvers.get("system-a")!(detail("system-a"))
    await openingA

    expect(session.state.route).toEqual({
      type: "run",
      runID: "shared-run",
      applicationID: "system-b",
      systemID: "system-b",
    })
    expect(session.state.runDetail?.summary.system_id).toBe("system-b")
  })

  test("analyzes the loaded failed Run with redacted evidence but a compact visible message", async () => {
    const failedRun = {
      run_id: "run-diagnose",
      system_id: null,
      application_id: "new",
      task_id: "task-diagnose",
      agent_name: "new_agent",
      status: "failed" as const,
      started_at: "2026-07-18T10:00:00Z",
      ended_at: "2026-07-18T10:00:05Z",
    }
    const detail: RunDetailResultDto = {
      summary: failedRun,
      error: "HTTP 401 from provider",
      workers: [],
      events: [{ type: "provider.response", raw: "EVENT_SECRET" }],
      logs: [{
        path: "logs/runtime.log",
        size: 128,
        tail: "Authorization: Bearer super-secret\n[ERROR] HTTP 401 request failed",
        tail_truncated: false,
      }],
      artifacts: [],
      result_state: "unavailable",
      result: null,
      limits: {
        workers: { truncated: false, returned_count: 0, max_count: 256 },
        events: {
          truncated: false,
          source_incomplete: false,
          returned_count: 1,
          returned_bytes: 64,
          max_count: 256,
          max_bytes: 262_144,
          max_scan_bytes: 1_048_576,
        },
        logs: {
          truncated: false,
          returned_count: 1,
          returned_bytes: 128,
          max_count: 16,
          max_bytes: 131_072,
          max_bytes_per_file: 16_384,
          max_scanned_entries: 4_096,
        },
        artifacts: {
          truncated: false,
          returned_count: 0,
          max_count: 256,
          max_scanned_entries: 4_096,
        },
        result: {
          truncated: false,
          source_incomplete: false,
          returned_bytes: 0,
          max_bytes: 262_144,
        },
      },
    }
    const client = new FakeClient()
    client.responses.set("run.detail", detail)
    client.responses.set("assistant.send", {
      session_id: "builder-1",
      assistant: "可能根因是模型凭据无效。",
      model_type: "powerful",
      draft: { revision: 0, valid: false, errors: [], files: [] },
    })
    const session = new AgentLoomSession({
      client,
      snapshot: { ...snapshot, systems: [], runs: [failedRun] },
      sessionID: "builder-1",
    })

    await session.openEntry(session.entries[0]!)
    await session.analyzeCurrentRun()

    const send = client.calls.find((call) => call.method === "assistant.send")!
    const providerMessage = String(send.params.message)
    expect(providerMessage).toContain("HTTP 401 from provider")
    expect(providerMessage).toContain("logs/runtime.log")
    expect(providerMessage).not.toContain("super-secret")
    expect(providerMessage).not.toContain("EVENT_SECRET")
    expect(session.state.route).toEqual({ type: "builder" })
    expect(session.state.messages.at(-2)?.content).toBe("分析 Run run-diagnose 的异常原因")
    expect(session.state.messages.at(-1)?.content).toBe("可能根因是模型凭据无效。")
    expect(session.state.messages.map((message) => message.content).join("\n")).not.toContain("logs/runtime.log")
  })
})
