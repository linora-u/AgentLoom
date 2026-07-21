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
    }

    expect((await new ApplicationStudioSessions(api).open("reports")).id).toBe("session-legacy")
  })
})
