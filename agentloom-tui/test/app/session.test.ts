import { describe, expect, test } from "bun:test"
import type {
  BootstrapResultDto,
  RpcMethod,
  RpcParams,
  RpcResult,
  RunDetailResultDto,
  SystemDetailResultDto,
} from "../../src/domain"
import { AgentLoomSession, type TuiClient } from "../../src/app/session"

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
  test("shows the sanitized configured model catalog without an RPC", async () => {
    const client = new FakeClient()
    const session = new AgentLoomSession({ client, snapshot, sessionID: "builder-1" })

    await session.refreshLive()
    await session.submit("/models")

    expect(client.calls).toEqual([])
    expect(session.state.messages.at(-1)?.content).toContain("powerful (默认) — Best quality")
    expect(session.state.messages.at(-1)?.content).toContain("fast — Lower latency")
    expect(session.state.messages.at(-1)?.content).not.toContain("api_key")
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
    client.responses.set("builder.send", {
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
        method: "builder.send",
        params: { session_id: "builder-1", message: "创建一个总结 Agent", model_type: "fast" },
      },
    ])
    expect(session.state.messages.at(-1)?.content).toBe("已生成草稿，请确认后 /apply。")
    expect(session.state.draft?.revision).toBe(3)
  })

  test("accepts a null model from the builder without discarding the user's selected model", async () => {
    const client = new FakeClient()
    client.responses.set("builder.send", {
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
    client.responses.set("builder.send", new Error("provider disconnected after staging"))
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

    expect(client.calls.map((call) => call.method)).toEqual(["builder.send", "builder.draft"])
    expect(session.state.notice).toBe("provider disconnected after staging")
    expect(session.state.draft?.revision).toBe(2)
    expect(session.state.draft?.files[0]?.content).toBe("name: recovered")
  })

  test("keeps a recovered Builder error visible across live refreshes", async () => {
    const client = new FakeClient()
    client.responses.set("builder.send", new Error("provider disconnected after staging"))
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
    client.responses.set("builder.send", {
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
    client.responses.set("builder.send", {
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
      "builder.send",
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
    const builder = deferred<RpcResult<"builder.send">>()
    const calls: string[] = []
    const client: TuiClient = {
      request<Method extends RpcMethod>(method: Method) {
        calls.push(method)
        if (method === "builder.send") {
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
    expect(calls.filter((method) => method === "builder.send")).toHaveLength(1)

    builder.resolve({
      session_id: "builder-1",
      assistant: "draft ready",
      model_type: "powerful",
      draft: { revision: 1, valid: true, errors: [], files: [] },
    })
    await sending

    expect(session.state.busy).toBe(false)
  })

  test("live refresh of an open Run directly reloads that Run without rescanning bootstrap", async () => {
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

    expect(calls).toEqual(["run.detail"])
    expect(session.state.snapshot.runs[0]!.status).toBe("completed")
    expect(session.state.snapshot.systems[0]!.state).toBe("completed")
    expect(session.state.snapshot.systems[0]!.latest_run?.status).toBe("completed")
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
})
