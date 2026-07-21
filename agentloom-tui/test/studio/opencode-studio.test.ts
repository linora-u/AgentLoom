import { describe, expect, test } from "bun:test"
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import type { OpenCodeSessionInfo } from "../../src/studio/application-sessions"
import type { OpenCodeStudioApi, OpenCodeStoredMessage } from "../../src/studio/opencode-studio"
import { OpenCodeStudioClient } from "../../src/studio/opencode-studio"

class MemoryStudioApi implements OpenCodeStudioApi {
  readonly prompts: Array<{ sessionID: string; message: string; system?: string; model?: { providerID: string; modelID: string } }> = []
  readonly permissionReplies: string[] = []
  readonly questionReplies: Array<{ requestID: string; answers?: string[][] }> = []
  readonly permissionUpdates: Array<{ sessionID: string; permission: unknown[] }> = []
  readonly aborts: string[] = []
  readonly deletedMessages: string[] = []
  readonly summaries: Array<{
    sessionID: string
    model: { providerID: string; modelID: string }
  }> = []
  hangPrompts = false
  failMessageReloadAfterSummary = false
  readonly workspaceKey?: string
  private clock = 1
  private readonly listeners = new Set<(event: any) => void>()
  private readonly sessions: OpenCodeSessionInfo[] = [{
    id: "ses_reports",
    title: "AgentLoom · reports",
    time: { created: 1, updated: 1 },
    metadata: { agentloom: { kind: "application-studio", application_id: "reports" } },
  }]
  private readonly histories = new Map<string, OpenCodeStoredMessage[]>([["ses_reports", [{
      info: { id: "msg-1", role: "assistant" },
      parts: [{ type: "text", text: "previous analysis" }],
    }]]])

  constructor(workspaceKey?: string) {
    this.workspaceKey = workspaceKey
    if (workspaceKey) {
      for (const session of this.sessions) {
        session.directory = workspaceKey
        session.metadata = {
          agentloom: {
            ...(session.metadata?.agentloom as Record<string, unknown>),
            workspace: workspaceKey,
          },
        }
      }
    }
  }

  async list() {
    return [...this.sessions]
  }

  async create(input: Parameters<OpenCodeStudioApi["create"]>[0]) {
    const now = ++this.clock
    const session = { id: `ses_${this.sessions.length + 1}`, time: { created: now, updated: now }, ...input }
    this.sessions.push(session)
    this.histories.set(session.id, [])
    return session
  }

  async update(sessionID: string, input: Parameters<OpenCodeStudioApi["update"]>[1]) {
    const index = this.sessions.findIndex((session) => session.id === sessionID)
    if (index < 0) throw new Error(`unknown session ${sessionID}`)
    const updated = {
      ...this.sessions[index]!,
      ...input,
      time: { ...this.sessions[index]!.time!, updated: ++this.clock },
    }
    this.sessions[index] = updated
    return updated
  }

  async messages(sessionID = "ses_reports") {
    if (this.failMessageReloadAfterSummary && this.summaries.length > 0) {
      throw new Error("message reload unavailable")
    }
    return [...(this.histories.get(sessionID) ?? [])]
  }

  async prompt(sessionID: string, message: string, system?: string, model?: { providerID: string; modelID: string }) {
    this.prompts.push({ sessionID, message, system, model })
    const history = this.histories.get(sessionID)
    if (!history) throw new Error(`unknown session ${sessionID}`)
    history.push(
      { info: { id: `${sessionID}-user-${history.length}`, role: "user" }, parts: [{ type: "text", text: message }] },
      { info: { id: `${sessionID}-assistant-${history.length + 1}`, role: "assistant" }, parts: [{ type: "text", text: "configuration updated" }] },
    )
    if (this.hangPrompts) await new Promise<void>(() => {})
  }

  async summarize(sessionID: string, model: { providerID: string; modelID: string }) {
    this.summaries.push({ sessionID, model })
  }

  subscribe(listener: (event: any) => void) {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  emit(event: any) {
    for (const listener of this.listeners) listener(event)
  }

  async replyPermission(requestID: string, reply: "once" | "always" | "reject") {
    this.permissionReplies.push(`${requestID}:${reply}`)
  }

  async replyQuestion(requestID: string, answers: string[][]) {
    this.questionReplies.push({ requestID, answers })
  }

  async rejectQuestion(requestID: string) {
    this.questionReplies.push({ requestID })
  }

  async setPermissions(sessionID: string, permission: any[]) {
    this.permissionUpdates.push({ sessionID, permission })
  }

  async models() {
    return [{
      id: "openai/gpt-5.4",
      providerID: "openai",
      modelID: "gpt-5.4",
      name: "GPT-5.4",
      providerName: "OpenAI",
      default: true,
    }]
  }

  async abort(sessionID: string) {
    this.aborts.push(sessionID)
  }

  async deleteMessage(sessionID: string, messageID: string) {
    this.deletedMessages.push(messageID)
    const history = this.histories.get(sessionID) ?? []
    const index = history.findIndex((message) => message.info.id === messageID)
    if (index >= 0) history.splice(index, 1)
  }

  appendHistory(...messages: OpenCodeStoredMessage[]) {
    this.histories.get("ses_reports")!.push(...messages)
  }

  seedApplicationSession(
    id: string,
    applicationID: string,
    updated: number,
    messages: OpenCodeStoredMessage[],
  ) {
    this.sessions.push({
      id,
      title: `AgentLoom · ${applicationID}`,
      time: { created: updated, updated },
      metadata: { agentloom: { kind: "application-studio", application_id: applicationID } },
      ...(this.workspaceKey ? { directory: this.workspaceKey } : {}),
    })
    this.histories.set(id, [...messages])
  }

  async close() {}
}

describe("OpenCode Studio client", () => {
  test("compacts the current Session with its selected model and keeps it resumable", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")

    const compacted = await studio.compact(opened.sessionID)
    const reopened = await studio.openApplication("reports")

    expect(api.summaries).toEqual([{
      sessionID: opened.sessionID,
      model: { providerID: "openai", modelID: "gpt-5.4" },
    }])
    expect(compacted).toEqual(opened)
    expect(reopened).toEqual(opened)
  })

  test("rejects manual compaction while the current Agent Loop is active", async () => {
    const api = new MemoryStudioApi()
    api.hangPrompts = true
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")
    const pending = studio.send(opened.sessionID, "keep working")
    await Bun.sleep(0)

    await expect(studio.compact(opened.sessionID)).rejects.toThrow("active Agent Loop")
    expect(api.summaries).toEqual([])

    await studio.interrupt(opened.sessionID)
    await expect(pending).rejects.toThrow("已中止")
  })

  test("reports compaction success when only the post-compact message reload fails", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")
    api.failMessageReloadAfterSummary = true

    const compacted = await studio.compact(opened.sessionID)

    expect(api.summaries).toHaveLength(1)
    expect(compacted).toEqual({
      sessionID: opened.sessionID,
      messages: [],
      historyRefreshFailed: true,
    })
  })

  test("/new starts blank while preserving every Application conversation in the memory index", async () => {
    const workspace = await mkdtemp(join(tmpdir(), "agentloom-studio-memory-"))
    try {
      const api = new MemoryStudioApi(workspace)
      const memoryRoot = join(workspace, "private-memory")
      const studio = new OpenCodeStudioClient(api, { memoryRoot })
      const opened = await studio.openApplication("reports")
      api.seedApplicationSession("ses_prior", "reports", 2, [{
        info: { id: "msg-prior", role: "assistant" },
        parts: [
          { type: "text", text: "keep the reviewer threshold at 0.9" },
          {
            type: "tool",
            tool: "agentloom_domain",
            state: {
              status: "completed",
              input: { action: "application.validate", params: { application_id: "reports" } },
              output: '{"valid":true,"api_key":"do-not-archive"}',
            },
          },
        ],
      }])

      const fresh = await studio.newSession(
        { type: "application", applicationID: "reports" },
        "application_only",
      )

      expect(fresh.sessionID).not.toBe(opened.sessionID)
      expect(fresh.messages).toEqual([])
      expect((await api.messages(opened.sessionID)).map((message) => message.info.id)).toEqual(["msg-1"])

      const updated = await studio.send(fresh.sessionID, "use the previous decision")
      const reopened = await studio.openApplication("reports")
      const capability = (await readdir(memoryRoot)).find((entry) => /^[a-f0-9]{64}$/.test(entry))!
      const applicationMemory = join(memoryRoot, capability)
      const generation = (await readdir(applicationMemory)).toSorted().at(-1)!
      const generationDirectory = join(applicationMemory, generation)
      const index = JSON.parse(await readFile(join(generationDirectory, "index.json"), "utf8")) as {
        schema_version: number
        conversations: Array<{ session_id: string; chunks: Array<{ file: string }> }>
      }
      const archive = (await Promise.all(index.conversations.flatMap((entry) => (
        entry.chunks.map((chunk) => readFile(join(generationDirectory, chunk.file), "utf8"))
      )))).join("\n")

      expect(updated.messages.map((message) => message.content)).toEqual([
        "use the previous decision",
        "configuration updated",
      ])
      expect(reopened).toEqual({ sessionID: fresh.sessionID, messages: updated.messages })
      expect(api.prompts[0]?.system).toContain("agentloom_memory")
      expect(api.prompts[0]?.system).toContain(`capability=${capability}`)
      expect(api.prompts[0]?.system).not.toContain(memoryRoot)
      expect(index.schema_version).toBe(2)
      expect(index.conversations.map((entry) => entry.session_id)).toContain("ses_reports")
      expect(index.conversations.map((entry) => entry.session_id)).toContain("ses_prior")
      expect(archive).toContain("previous analysis")
      expect(archive).toContain("keep the reviewer threshold at 0.9")
      expect(archive).toContain("Tool: agentloom_domain · completed")
      expect(archive).toContain('"valid":true')
      expect(archive).not.toContain("do-not-archive")
    } finally {
      await rm(workspace, { recursive: true, force: true })
    }
  })

  test("aborts a stalled parent turn and its Task subagent instead of staying busy forever", async () => {
    const api = new MemoryStudioApi()
    api.hangPrompts = true
    const studio = new OpenCodeStudioClient(api, { stallTimeoutMs: 20 })
    const opened = await studio.openApplication("reports")
    const pending = studio.send(opened.sessionID, "what does this application do?")
    await Bun.sleep(0)
    api.emit({
      type: "tool",
      sessionID: opened.sessionID,
      callID: "task-1",
      name: "task",
      status: "running",
      metadata: { sessionId: "ses_child" },
    })

    const outcome = await Promise.race([
      pending.then(() => "completed", (error) => String(error instanceof Error ? error.message : error)),
      Bun.sleep(200).then(() => "still-busy"),
    ])

    expect(outcome).toContain("无进展")
    expect(api.aborts.sort()).toEqual(["ses_child", "ses_reports"])
    expect(api.deletedMessages).toEqual(["ses_reports-assistant-2", "ses_reports-user-1"])
    expect((await api.messages("ses_reports")).map((message) => message.info.id)).toEqual(["msg-1"])
  })

  test("projects Task subagent progress into the visible parent Studio session", async () => {
    const api = new MemoryStudioApi()
    api.hangPrompts = true
    const studio = new OpenCodeStudioClient(api, { stallTimeoutMs: 500 })
    const opened = await studio.openApplication("reports")
    const observed: unknown[] = []
    studio.subscribe?.((event) => observed.push(event))
    const pending = studio.send(opened.sessionID, "review every dataset row")

    api.emit({
      type: "tool",
      sessionID: opened.sessionID,
      callID: "task-1",
      name: "task",
      status: "running",
      title: "Review row 1",
      metadata: { sessionId: "ses_child" },
    })
    api.emit({
      type: "tool",
      sessionID: "ses_child",
      callID: "read-1",
      name: "read",
      status: "completed",
      title: "dataset row 1",
    })
    api.emit({ type: "text.delta", sessionID: "ses_child", text: "row 1 is correct" })

    expect(observed).toContainEqual({
      type: "tool",
      sessionID: opened.sessionID,
      callID: "read-1",
      name: "read",
      status: "completed",
      title: "dataset row 1",
      source: { kind: "subagent", sessionID: "ses_child" },
    })
    expect(observed).toContainEqual({
      type: "text.delta",
      sessionID: opened.sessionID,
      text: "row 1 is correct",
      source: { kind: "subagent", sessionID: "ses_child" },
    })

    await studio.interrupt?.(opened.sessionID)
    await pending.catch(() => undefined)
  })

  test("manual interrupt discards unfinished model context and settles the turn", async () => {
    const api = new MemoryStudioApi()
    api.hangPrompts = true
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")
    const pending = studio.send(opened.sessionID, "inspect every agent")
    await Bun.sleep(0)

    await studio.interrupt?.(opened.sessionID)
    const outcome = await pending.then(
      () => "completed",
      (error) => String(error instanceof Error ? error.message : error),
    )

    expect(outcome).toContain("已中止")
    expect(api.aborts).toEqual(["ses_reports"])
    expect(api.deletedMessages).toEqual(["ses_reports-assistant-2", "ses_reports-user-1"])
    expect((await api.messages("ses_reports")).map((message) => message.info.id)).toEqual(["msg-1"])
  })

  test("opening a persistent session removes pre-fix aborted turns", async () => {
    const api = new MemoryStudioApi()
    api.appendHistory(
      { info: { id: "msg-old-user", role: "user" }, parts: [{ type: "text", text: "resume the old task" }] },
      { info: { id: "msg-old-tool-step", role: "assistant" }, parts: [{ type: "tool" }] },
      { info: { id: "msg-old-assistant", role: "assistant", errorName: "MessageAbortedError" }, parts: [] },
    )
    const studio = new OpenCodeStudioClient(api)

    const opened = await studio.openApplication("reports")

    expect(opened.messages).toEqual([{ id: "msg-1", role: "assistant", content: "previous analysis" }])
    expect(api.deletedMessages).toEqual(["msg-old-assistant", "msg-old-tool-step", "msg-old-user"])
  })

  test("resumes history and returns the completed OpenCode turn", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)

    const opened = await studio.openApplication("reports")
    const updated = await studio.send(opened.sessionID, "add a reviewer")

    expect(opened).toEqual({
      sessionID: "ses_reports",
      messages: [{ id: "msg-1", role: "assistant", content: "previous analysis" }],
    })
    expect(updated.messages).toEqual([
      { id: "msg-1", role: "assistant", content: "previous analysis" },
      { id: "ses_reports-user-1", role: "user", content: "add a reviewer" },
      { id: "ses_reports-assistant-2", role: "assistant", content: "configuration updated" },
    ])
    expect(api.prompts[0]?.system).toContain("applications/reports")
    expect(api.prompts[0]?.system).toContain("agentloom-framework-skill")
    expect(api.prompts[0]?.system).toContain("agentloom_domain")
    expect(api.prompts[0]?.system).toContain("修改 → 静态校验 → 冒烟运行")
    expect(api.prompts[0]?.system).toContain("最新一条用户消息是当前唯一任务")
    expect(api.prompts[0]?.system).toContain("MessageAbortedError")
    expect(api.prompts[0]?.system).not.toContain("/apply")
  })

  test("forwards OpenCode loop events and permission replies", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)
    const observed: unknown[] = []
    const unsubscribe = studio.subscribe?.((event) => observed.push(event))

    api.emit({
      type: "permission",
      sessionID: "ses_reports",
      requestID: "per_1",
      permission: "bash",
      patterns: ["loom run *"],
      metadata: { command: "loom run app.yaml" },
    })
    await studio.replyPermission?.("per_1", "always")
    unsubscribe?.()

    expect(observed).toEqual([{
      type: "permission",
      sessionID: "ses_reports",
      requestID: "per_1",
      permission: "bash",
      patterns: ["loom run *"],
      metadata: { command: "loom run app.yaml" },
    }])
    expect(api.permissionReplies).toEqual(["per_1:always"])
  })

  test("replies to and rejects OpenCode question requests", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)

    await studio.replyQuestion?.("que_1", [["Create a new Worker"]])
    await studio.rejectQuestion?.("que_2")

    expect(api.questionReplies).toEqual([
      { requestID: "que_1", answers: [["Create a new Worker"]] },
      { requestID: "que_2" },
    ])
  })

  test("Full Access is an explicit OpenCode session-only permission override", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")

    await studio.setPermissionMode(opened.sessionID, "full_access")
    await studio.setPermissionMode(opened.sessionID, "application_only")

    expect(api.permissionUpdates).toEqual([
      {
        sessionID: "ses_reports",
        permission: [{ permission: "*", pattern: "*", action: "allow" }],
      },
      {
        sessionID: "ses_reports",
        permission: [
          { permission: "edit", pattern: "*", action: "ask" },
          { permission: "edit", pattern: "applications/reports", action: "allow" },
          { permission: "edit", pattern: "applications/reports/*", action: "allow" },
          { permission: "bash", pattern: "*", action: "ask" },
          { permission: "external_directory", pattern: "*", action: "ask" },
          { permission: "agentloom_run", pattern: "*", action: "ask" },
        ],
      },
    ])
  })

  test("lists connected OpenCode models and applies selection only to Studio prompts", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")

    const models = await studio.listModels()
    await studio.setModel(opened.sessionID, models[0]!.id)
    await studio.send(opened.sessionID, "use the selected Studio model")

    expect(models[0]).toEqual({
      id: "openai/gpt-5.4",
      providerID: "openai",
      modelID: "gpt-5.4",
      name: "GPT-5.4",
      providerName: "OpenAI",
      default: true,
    })
    expect(api.prompts.at(-1)?.model).toEqual({ providerID: "openai", modelID: "gpt-5.4" })
  })

  test("pins the llm.yaml default on every resumed Studio Session prompt", async () => {
    const api = new MemoryStudioApi()
    const studio = new OpenCodeStudioClient(api)
    const opened = await studio.openApplication("reports")

    await studio.send(opened.sessionID, "ignore the model persisted by the old OpenCode session")

    expect(api.prompts.at(-1)?.model).toEqual({ providerID: "openai", modelID: "gpt-5.4" })
  })
})
