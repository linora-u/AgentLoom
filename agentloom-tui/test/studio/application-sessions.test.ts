import { describe, expect, test } from "bun:test"
import {
  ApplicationStudioSessions,
  type OpenCodeSessionApi,
  type OpenCodeSessionInfo,
} from "../../src/studio/application-sessions"

class MemoryOpenCodeSessions implements OpenCodeSessionApi {
  private readonly sessions: OpenCodeSessionInfo[]

  constructor(initial: OpenCodeSessionInfo[] = []) {
    this.sessions = [...initial]
  }

  async list() {
    return [...this.sessions]
  }

  async create(input: Parameters<OpenCodeSessionApi["create"]>[0]) {
    const session = {
      id: `session-${this.sessions.length + 1}`,
      ...input,
    }
    this.sessions.push(session)
    return session
  }

  async update(sessionID: string, input: Parameters<OpenCodeSessionApi["update"]>[1]) {
    const index = this.sessions.findIndex((session) => session.id === sessionID)
    if (index < 0) throw new Error(`unknown session ${sessionID}`)
    const updated = { ...this.sessions[index]!, ...input }
    this.sessions[index] = updated
    return updated
  }

  async all() {
    return [...this.sessions]
  }
}

describe("Application Studio sessions", () => {
  test("resumes the most recently updated conversation for an Application", async () => {
    const older = {
      id: "session-older",
      title: "AgentLoom · reports",
      metadata: { agentloom: { kind: "application-studio", application_id: "reports" } },
      time: { created: 10, updated: 20 },
    } as OpenCodeSessionInfo
    const latest = {
      id: "session-latest",
      title: "AgentLoom · reports",
      metadata: { agentloom: { kind: "application-studio", application_id: "reports" } },
      time: { created: 30, updated: 40 },
    } as OpenCodeSessionInfo
    const sessions = new ApplicationStudioSessions(new MemoryOpenCodeSessions([older, latest]))

    expect((await sessions.open("reports")).id).toBe("session-latest")
  })

  test("each Application resumes its own persistent OpenCode session", async () => {
    const api = new MemoryOpenCodeSessions()
    const sessions = new ApplicationStudioSessions(api)

    const alphaFirst = await sessions.open("alpha")
    const beta = await sessions.open("beta")
    const alphaAgain = await sessions.open("alpha")

    expect(alphaAgain.id).toBe(alphaFirst.id)
    expect(beta.id).not.toBe(alphaFirst.id)
    expect(await api.all()).toEqual([
      {
        id: "session-1",
        title: "AgentLoom · alpha",
        metadata: { agentloom: { kind: "application-studio", application_id: "alpha" } },
        permission: [
          { permission: "edit", pattern: "*", action: "ask" },
          { permission: "edit", pattern: "applications/alpha", action: "allow" },
          { permission: "edit", pattern: "applications/alpha/*", action: "allow" },
          { permission: "bash", pattern: "*", action: "ask" },
          { permission: "external_directory", pattern: "*", action: "ask" },
          { permission: "agentloom_run", pattern: "*", action: "ask" },
        ],
      },
      {
        id: "session-2",
        title: "AgentLoom · beta",
        metadata: { agentloom: { kind: "application-studio", application_id: "beta" } },
        permission: [
          { permission: "edit", pattern: "*", action: "ask" },
          { permission: "edit", pattern: "applications/beta", action: "allow" },
          { permission: "edit", pattern: "applications/beta/*", action: "allow" },
          { permission: "bash", pattern: "*", action: "ask" },
          { permission: "external_directory", pattern: "*", action: "ask" },
          { permission: "agentloom_run", pattern: "*", action: "ask" },
        ],
      },
    ])
  })

  test("New Application always starts an isolated session with writes requiring approval", async () => {
    const api = new MemoryOpenCodeSessions()
    const sessions = new ApplicationStudioSessions(api)

    const first = await sessions.openNew()
    const second = await sessions.openNew()

    expect(second.id).not.toBe(first.id)
    expect(first.title).toBe("AgentLoom · New Application")
    expect(first.metadata).toMatchObject({ agentloom: { kind: "application-studio-new" } })
    expect(first.permission).toEqual([
      { permission: "edit", pattern: "*", action: "ask" },
      { permission: "bash", pattern: "*", action: "ask" },
      { permission: "external_directory", pattern: "*", action: "ask" },
      { permission: "agentloom_run", pattern: "*", action: "ask" },
    ])
  })

  test("claims the New Application conversation as the created Application", async () => {
    const api = new MemoryOpenCodeSessions()
    const sessions = new ApplicationStudioSessions(api)
    const opened = await sessions.openNew()

    const claimed = await sessions.claim(
      opened.id,
      "beta",
      "full_access",
    )

    expect(claimed.id).toBe(opened.id)
    expect(claimed.title).toBe("AgentLoom · beta")
    expect(claimed.metadata).toEqual({
      agentloom: {
        kind: "application-studio",
        application_id: "beta",
      },
    })
    expect(claimed.permission).toEqual([
      { permission: "*", pattern: "*", action: "allow" },
    ])
    expect((await sessions.open("beta")).id).toBe(opened.id)
  })

  test("does not resume a same-named Application session from another workspace", async () => {
    let created: OpenCodeSessionInfo | undefined
    const api: OpenCodeSessionApi = {
      workspaceKey: "/projects/current",
      async list() {
        return [{
          id: "session-other-project",
          title: "AgentLoom · reports",
          directory: "/projects/other",
          metadata: {
            agentloom: {
              kind: "application-studio",
              application_id: "reports",
              workspace: "/projects/other",
            },
          },
        }]
      },
      async create(input) {
        created = { id: "session-current-project", directory: "/projects/current", ...input }
        return created
      },
      async update() { throw new Error("not expected") },
    }

    const opened = await new ApplicationStudioSessions(api).open("reports")

    expect(opened.id).toBe("session-current-project")
    expect(created?.metadata).toMatchObject({
      agentloom: { application_id: "reports", workspace: "/projects/current" },
    })
  })

  test("resumes a legacy session only when its persisted directory matches", async () => {
    const legacy: OpenCodeSessionInfo = {
      id: "session-legacy",
      title: "AgentLoom · reports",
      directory: "/projects/current",
      metadata: { agentloom: { kind: "application-studio", application_id: "reports" } },
    }
    const api: OpenCodeSessionApi = {
      workspaceKey: "/projects/current",
      async list() { return [legacy] },
      async create() { throw new Error("legacy session should have resumed") },
      async update(sessionID, input) { return { ...legacy, id: sessionID, ...input } },
    }

    expect((await new ApplicationStudioSessions(api).open("reports")).id).toBe("session-legacy")
  })
})
