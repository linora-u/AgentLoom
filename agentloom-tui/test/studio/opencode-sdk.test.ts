import { afterEach, describe, expect, test } from "bun:test"
import { ApplicationStudioSessions } from "../../src/studio/application-sessions"
import { createOpenCodeSessionApi } from "../../src/studio/opencode-sdk"
import { OpenCodeStudioClient, type OpenCodeStoredMessage } from "../../src/studio/opencode-studio"

const servers: Array<{ stop(closeActiveConnections?: boolean): void }> = []

afterEach(() => {
  for (const server of servers.splice(0)) server.stop(true)
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
    for (let index = 0; index < 20 && received.length < 3; index += 1) await Bun.sleep(5)
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

  test("Application sessions are persisted through the OpenCode Session API", async () => {
    const stored: Array<{ id: string; title: string; metadata: Record<string, unknown>; permission?: unknown[] }> = []
    const directories: string[] = []
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url)
        if (!url.pathname.startsWith("/session")) return new Response("not found", { status: 404 })
        directories.push(url.searchParams.get("directory") ?? "")
        if (url.pathname === "/session" && request.method === "GET") return Response.json(stored)
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

    expect(resumed.id).toBe(first.id)
    expect(stored).toEqual([{
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
    }])
    expect(directories).toEqual(["/repo", "/repo", "/repo", "/repo"])
  })

  test("Studio prompts and reloads messages through the OpenCode SDK", async () => {
    const session = {
      id: "ses_reports",
      title: "AgentLoom · reports",
      directory: "/repo",
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
    const studio = new OpenCodeStudioClient(createOpenCodeSessionApi({
      baseUrl: `http://127.0.0.1:${server.port}`,
      directory: "/repo",
      models: [{
        id: "configured",
        modelID: "configured",
        providerID: "agentloom-cG93ZXJmdWw",
        providerName: "powerful",
        name: "powerful",
        default: true,
      }],
    }))

    const opened = await studio.openApplication("reports")
    const updated = await studio.send(opened.sessionID, "change config")

    expect(updated.messages.at(-2)).toEqual({ id: "msg-2", role: "user", content: "change config" })
    expect(updated.messages.at(-1)).toEqual({ id: "msg-3", role: "assistant", content: "done" })
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
