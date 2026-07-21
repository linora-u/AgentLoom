import { describe, expect, test } from "bun:test"
import {
  ApplicationStudioSessions,
  type OpenCodeSessionApi,
  type OpenCodeSessionInfo,
} from "../../src/studio/application-sessions"

class MemoryOpenCodeSessions implements OpenCodeSessionApi {
  private readonly sessions: OpenCodeSessionInfo[] = []

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

  test("retargets one conversation without losing its Session identity or Full Access mode", async () => {
    const api = new MemoryOpenCodeSessions()
    const sessions = new ApplicationStudioSessions(api)
    const opened = await sessions.open("alpha")

    const switched = await sessions.retarget(
      opened.id,
      { type: "application", applicationID: "beta" },
      "full_access",
    )

    expect(switched.id).toBe(opened.id)
    expect(switched.title).toBe("AgentLoom · beta")
    expect(switched.metadata).toEqual({
      agentloom: { kind: "application-studio", application_id: "beta" },
    })
    expect(switched.permission).toEqual([
      { permission: "*", pattern: "*", action: "allow" },
    ])
    expect(await api.all()).toHaveLength(1)
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
