import { afterEach, describe, expect, test } from "bun:test"
import { mkdtemp, realpath, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { ApplicationStudioSessions } from "../../src/studio/application-sessions"
import { createOpenCodeSessionApi } from "../../src/studio/opencode-sdk"
import { OpenCodeStudioClient, type OpenCodeStoredMessage } from "../../src/studio/opencode-studio"

const servers: Array<{ stop(closeActiveConnections?: boolean): void }> = []
const temporaryDirectories: string[] = []

afterEach(async () => {
  for (const server of servers.splice(0)) server.stop(true)
  for (const directory of temporaryDirectories.splice(0)) {
    await rm(directory, { recursive: true, force: true })
  }
})

describe("OpenCode SDK boundary", () => {
  test("closing the API cancels the long-lived OpenCode event stream", async () => {
    let markSubscribed!: () => void
    let markCancelled!: () => void
    const subscribed = new Promise<void>((resolve) => { markSubscribed = resolve })
    const cancelled = new Promise<void>((resolve) => { markCancelled = resolve })
    const server = Bun.serve({
      port: 0,
      fetch(request) {
        const url = new URL(request.url)
        if (url.pathname !== "/event") return new Response("not found", { status: 404 })
        markSubscribed()
        return new Response(new ReadableStream({
          cancel() {
            markCancelled()
          },
        }), {
          headers: { "content-type": "text/event-stream" },
        })
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })
    api.subscribe(() => {})

    await subscribed
    const closeStartedAt = performance.now()
    await api.close()
    const closeDurationMs = performance.now() - closeStartedAt

    await expect(Promise.race([
      cancelled.then(() => "cancelled"),
      Bun.sleep(1_000).then(() => "timed-out"),
    ])).resolves.toBe("cancelled")
    expect(closeDurationMs).toBeLessThan(500)
  })

  test("streams final text parts but never exposes model reasoning parts", async () => {
    let markSubscribed!: () => void
    const subscribed = new Promise<void>((resolve) => { markSubscribed = resolve })
    const events = [
      {
        type: "message.part.updated",
        properties: {
          sessionID: "ses_reasoning",
          part: { id: "part_reasoning", type: "reasoning", sessionID: "ses_reasoning", messageID: "msg_1", text: "" },
        },
      },
      {
        type: "message.part.delta",
        properties: {
          sessionID: "ses_reasoning",
          messageID: "msg_1",
          partID: "part_reasoning",
          field: "text",
          delta: "private chain of thought",
        },
      },
      {
        type: "message.part.updated",
        properties: {
          sessionID: "ses_reasoning",
          part: { id: "part_text", type: "text", sessionID: "ses_reasoning", messageID: "msg_1", text: "" },
        },
      },
      {
        type: "message.part.delta",
        properties: {
          sessionID: "ses_reasoning",
          messageID: "msg_1",
          partID: "part_text",
          field: "text",
          delta: "public answer",
        },
      },
    ]
    const payload = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")
    const server = Bun.serve({
      port: 0,
      fetch(request) {
        if (new URL(request.url).pathname !== "/event") return new Response("not found", { status: 404 })
        markSubscribed()
        return new Response(payload, { headers: { "content-type": "text/event-stream" } })
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })
    const received: string[] = []
    api.subscribe((event) => {
      if (event.type === "text.delta") received.push(event.text)
    })

    await subscribed
    for (let index = 0; index < 20 && received.length === 0; index += 1) await Bun.sleep(5)
    await api.close()

    expect(received).toEqual(["public answer"])
  })

  test("maps durable OpenCode event envelopes used by the bundled runtime", async () => {
    let markSubscribed!: () => void
    const subscribed = new Promise<void>((resolve) => { markSubscribed = resolve })
    const events = [
      {
        id: "evt_part",
        type: "message.part.updated",
        data: {
          sessionID: "ses_parent",
          time: 1,
          part: { id: "part_text", type: "text", sessionID: "ses_parent", messageID: "msg_1", text: "" },
        },
      },
      {
        id: "evt_delta",
        type: "message.part.delta",
        data: {
          sessionID: "ses_parent",
          messageID: "msg_1",
          partID: "part_text",
          field: "text",
          delta: "visible progress",
        },
      },
      {
        id: "evt_permission",
        type: "permission.asked",
        data: {
          id: "per_1",
          sessionID: "ses_parent",
          permission: "bash",
          patterns: ["head dataset.csv"],
          metadata: {},
          always: [],
        },
      },
      {
        id: "evt_task",
        type: "message.part.updated",
        data: {
          sessionID: "ses_parent",
          time: 2,
          part: {
            id: "part_task",
            type: "tool",
            sessionID: "ses_parent",
            messageID: "msg_1",
            callID: "call_task",
            tool: "task",
            state: { status: "running", metadata: { sessionId: "ses_child" } },
          },
        },
      },
      {
        id: "evt_compaction_started",
        type: "session.next.compaction.started",
        data: {
          sessionID: "ses_parent",
          messageID: "msg_compaction",
          reason: "auto",
          timestamp: 3,
        },
      },
      {
        id: "evt_compaction_ended",
        type: "session.next.compaction.ended",
        data: {
          sessionID: "ses_parent",
          messageID: "msg_compaction",
          reason: "auto",
          timestamp: 4,
          text: "summary",
          recent: "recent",
        },
      },
    ]
    const payload = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")
    const server = Bun.serve({
      port: 0,
      fetch(request) {
        if (new URL(request.url).pathname !== "/event") return new Response("not found", { status: 404 })
        markSubscribed()
        return new Response(payload, { headers: { "content-type": "text/event-stream" } })
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })
    const received: unknown[] = []
    api.subscribe((event) => received.push(event))

    await subscribed
    for (let index = 0; index < 20 && received.length < 5; index += 1) await Bun.sleep(5)
    await api.close()

    expect(received).toEqual([
      { type: "text.delta", sessionID: "ses_parent", text: "visible progress" },
      {
        type: "permission",
        sessionID: "ses_parent",
        requestID: "per_1",
        permission: "bash",
        patterns: ["head dataset.csv"],
        metadata: {},
      },
      {
        type: "tool",
        sessionID: "ses_parent",
        callID: "call_task",
        name: "task",
        status: "running",
        metadata: { sessionId: "ses_child" },
      },
      {
        type: "compaction",
        sessionID: "ses_parent",
        phase: "started",
        reason: "auto",
      },
      {
        type: "compaction",
        sessionID: "ses_parent",
        phase: "completed",
        reason: "auto",
      },
    ])
  })

  test("question answers and rejection use the official OpenCode endpoints", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = []
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url)
        requests.push({
          path: `${url.pathname}${url.search}`,
          method: request.method,
          body: request.headers.get("content-length") === "0" ? null : await request.json().catch(() => null),
        })
        return Response.json(true)
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })

    await api.replyQuestion("que_1", [["Supervisor + Workers"]])
    await api.rejectQuestion("que_2")

    expect(requests).toEqual([
      {
        path: "/question/que_1/reply?directory=%2Frepo",
        method: "POST",
        body: { answers: [["Supervisor + Workers"]] },
      },
      {
        path: "/question/que_2/reject?directory=%2Frepo",
        method: "POST",
        body: null,
      },
    ])
  })

  test("manual compaction uses the official Session summarize endpoint and selected model", async () => {
    const requests: Array<{ path: string; method: string; body: unknown }> = []
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url)
        requests.push({
          path: `${url.pathname}${url.search}`,
          method: request.method,
          body: await request.json().catch(() => null),
        })
        return Response.json(true)
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })

    await api.summarize("ses_reports", { providerID: "openai", modelID: "gpt-5.4" })

    expect(requests).toEqual([{
      path: "/session/ses_reports/summarize?directory=%2Frepo",
      method: "POST",
      body: { providerID: "openai", modelID: "gpt-5.4", auto: false },
    }])
  })

  test("Application sessions are persisted through the OpenCode Session API", async () => {
    const stored: Array<{
      id: string
      title: string
      metadata: Record<string, unknown>
      permission?: unknown[]
      parentID?: string
      time?: { created: number; updated: number }
    }> = []
    const directories: string[] = []
    const roots: string[] = []
    const limits: string[] = []
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url)
        if (!url.pathname.startsWith("/session")) return new Response("not found", { status: 404 })
        directories.push(url.searchParams.get("directory") ?? "")
        if (url.pathname === "/session" && request.method === "GET") {
          roots.push(url.searchParams.get("roots") ?? "")
          limits.push(url.searchParams.get("limit") ?? "")
          return Response.json(stored)
        }
        if (url.pathname === "/session" && request.method === "POST") {
          const body = await request.json() as { title: string; metadata: Record<string, unknown> }
          const session = { id: `sdk-${stored.length + 1}`, ...body }
          stored.push(session)
          return Response.json(session)
        }
        if (request.method === "PATCH") {
          const body = await request.json() as Omit<(typeof stored)[number], "id">
          const index = stored.findIndex((session) => url.pathname === `/session/${session.id}`)
          if (index < 0) return new Response("not found", { status: 404 })
          stored[index] = { ...stored[index]!, ...body }
          return Response.json(stored[index])
        }
        return new Response("method not allowed", { status: 405 })
      },
    })
    servers.push(server)
    const sessions = new ApplicationStudioSessions(createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    }))

    const first = await sessions.open("reports")
    const resumed = await sessions.open("reports")
    const branched = await sessions.createFresh(
      { type: "application", applicationID: "reports" },
      "full_access",
    )

    expect(resumed.id).toBe(first.id)
    expect(branched).toMatchObject({ id: "sdk-2" })
    expect(stored[0]).toEqual({
      id: "sdk-1",
      title: "AgentLoom · reports",
      metadata: {
        agentloom: {
          kind: "application-studio",
          application_id: "reports",
          workspace: "/repo",
        },
      },
      permission: [
        { permission: "edit", pattern: "*", action: "ask" },
        { permission: "edit", pattern: "applications/reports", action: "allow" },
        { permission: "edit", pattern: "applications/reports/*", action: "allow" },
        { permission: "edit", pattern: "/repo/applications/reports", action: "allow" },
        { permission: "edit", pattern: "/repo/applications/reports/*", action: "allow" },
        { permission: "edit", pattern: "repo/applications/reports", action: "allow" },
        { permission: "edit", pattern: "repo/applications/reports/*", action: "allow" },
        { permission: "bash", pattern: "*", action: "ask" },
        { permission: "external_directory", pattern: "*", action: "ask" },
        { permission: "agentloom_run", pattern: "*", action: "ask" },
      ],
    })
    expect(stored[1]).toMatchObject({
      id: "sdk-2",
      metadata: {
        agentloom: {
          kind: "application-studio",
          application_id: "reports",
          workspace: "/repo",
        },
      },
      permission: [{ permission: "*", pattern: "*", action: "allow" }],
    })
    expect(roots).toEqual(["true", "true"])
    expect(limits).toEqual(["100001", "100001"])
    expect(directories).toEqual(["/repo", "/repo", "/repo", "/repo", "/repo"])
  })

  test("restores an Application even when more than 100 newer root sessions exist", async () => {
    const target = {
      id: "ses_target",
      title: "AgentLoom · reports",
      directory: "/repo",
      time: { created: 1, updated: 1 },
      metadata: { agentloom: { kind: "application-studio", application_id: "reports" } },
    }
    const newer = Array.from({ length: 125 }, (_, index) => ({
      id: `ses_unrelated_${index}`,
      title: `Unrelated ${index}`,
      directory: "/repo",
      time: { created: index + 2, updated: index + 2 },
      metadata: { agentloom: { kind: "application-studio", application_id: `other_${index}` } },
    }))
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url)
        if (url.pathname === "/session" && request.method === "GET") {
          const limit = Number(url.searchParams.get("limit") ?? 100)
          return Response.json([...newer.toReversed(), target].slice(0, limit))
        }
        if (url.pathname === "/session/ses_target" && request.method === "PATCH") {
          return Response.json({ ...target, ...(await request.json() as object) })
        }
        return new Response("not found", { status: 404 })
      },
    })
    servers.push(server)
    const sessions = new ApplicationStudioSessions(createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    }))

    expect((await sessions.open("reports")).id).toBe("ses_target")
  })

  test("Studio prompts and reloads messages through the OpenCode SDK", async () => {
    const projectRoot = await realpath(await mkdtemp(join(tmpdir(), "agentloom-sdk-memory-")))
    temporaryDirectories.push(projectRoot)
    const session = {
      id: "ses_reports",
      title: "AgentLoom · reports",
      directory: projectRoot,
      metadata: { agentloom: { kind: "application-studio", application_id: "reports" } },
    }
    const history: OpenCodeStoredMessage[] = [{
      info: { id: "msg-1", role: "assistant" },
      parts: [{ type: "text", text: "ready" }],
    }]
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url)
        if (url.pathname === "/session" && request.method === "GET") return Response.json([session])
        if (url.pathname === "/session/ses_reports" && request.method === "PATCH") {
          return Response.json({ ...session, ...(await request.json() as object) })
        }
        if (url.pathname === "/session/ses_reports/message" && request.method === "GET") {
          return Response.json(history)
        }
        if (url.pathname === "/session/ses_reports/message" && request.method === "POST") {
          const body = await request.json() as { parts: Array<{ type: string; text: string }> }
          const text = body.parts.find((part) => part.type === "text")?.text ?? ""
          history.push(
            { info: { id: "msg-2", role: "user" }, parts: [{ type: "text", text }] },
            { info: { id: "msg-3", role: "assistant" }, parts: [{ type: "text", text: "done" }] },
          )
          return Response.json(history.at(-1))
        }
        return new Response("not found", { status: 404 })
      },
    })
    servers.push(server)
    const studio = new OpenCodeStudioClient(
      createOpenCodeSessionApi({
        baseUrl: `http://127.0.0.1:${server.port}`,
        directory: projectRoot,
        models: [{
          id: "configured",
          modelID: "configured",
          providerID: "agentloom-cG93ZXJmdWw",
          providerName: "powerful",
          name: "powerful",
          default: true,
        }],
      }),
      { memoryRoot: join(projectRoot, "private-memory") },
    )

    const opened = await studio.openApplication("reports")
    const updated = await studio.send(opened.sessionID, "change config")

    expect(updated.messages.at(-2)).toEqual({ id: "msg-2", role: "user", content: "change config" })
    expect(updated.messages.at(-1)).toEqual({ id: "msg-3", role: "assistant", content: "done" })
  })

  test("preserves tool inputs, results, and errors for the Application memory archive", async () => {
    const server = Bun.serve({
      port: 0,
      fetch(request) {
        if (new URL(request.url).pathname !== "/session/ses_tools/message") {
          return new Response("not found", { status: 404 })
        }
        return Response.json([{
          info: { id: "msg_tools", role: "assistant" },
          parts: [
            {
              id: "part_tool",
              sessionID: "ses_tools",
              messageID: "msg_tools",
              type: "tool",
              callID: "call_validate",
              tool: "agentloom_domain",
              state: {
                status: "completed",
                input: { action: "application.validate" },
                output: '{"valid":true}',
                title: "validate reports",
              },
            },
            {
              id: "part_error",
              sessionID: "ses_tools",
              messageID: "msg_tools",
              type: "tool",
              callID: "call_run",
              tool: "agentloom_domain",
              state: {
                status: "error",
                input: { action: "run.start" },
                error: "run failed",
              },
            },
          ],
        }])
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })

    expect(await api.messages("ses_tools")).toEqual([{
      info: { id: "msg_tools", role: "assistant" },
      parts: [
        {
          type: "tool",
          tool: "agentloom_domain",
          state: {
            status: "completed",
            title: "validate reports",
            input: { action: "application.validate" },
            output: '{"valid":true}',
          },
        },
        {
          type: "tool",
          tool: "agentloom_domain",
          state: {
            status: "error",
            input: { action: "run.start" },
            error: "run failed",
          },
        },
      ],
    }])
  })

  test("discarding an interrupted turn uses OpenCode message deletion without a file revert", async () => {
    const requests: Array<{ path: string; method: string }> = []
    const server = Bun.serve({
      port: 0,
      fetch(request) {
        const url = new URL(request.url)
        requests.push({ path: `${url.pathname}${url.search}`, method: request.method })
        return Response.json(true)
      },
    })
    servers.push(server)
    const api = createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
    })

    await api.deleteMessage("ses_reports", "msg_aborted")

    expect(requests).toEqual([{
      path: "/session/ses_reports/message/msg_aborted?directory=%2Frepo",
      method: "DELETE",
    }])
  })
})
