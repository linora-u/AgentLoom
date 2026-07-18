import { describe, expect, test } from "bun:test"
import {
  BridgeClient,
  BridgeClosedError,
  BridgeRpcError,
  type BridgeTransport,
  type BridgeTransportHandlers,
} from "../../src/bridge/client"

class FakeTransport implements BridgeTransport {
  readonly sent: string[] = []
  closed = false
  private handlers?: BridgeTransportHandlers

  async start(handlers: BridgeTransportHandlers) {
    this.handlers = handlers
  }

  async send(line: string) {
    this.sent.push(line)
  }

  async close() {
    this.closed = true
  }

  receive(value: unknown) {
    this.handlers?.line(JSON.stringify(value))
  }
}

describe("BridgeClient", () => {
  test("correlates concurrent responses by request id", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, { createID: sequentialIDs("system", "run") })

    const system = client.systemDetail("research")
    const run = client.runDetail("run-1", "reports", "research")
    await waitForRequests(transport, 2)

    expect(JSON.parse(transport.sent[0]!)).toEqual({
      id: "system",
      method: "system.detail",
      params: { system_id: "research" },
    })
    expect(JSON.parse(transport.sent[1]!)).toEqual({
      id: "run",
      method: "run.detail",
      params: { run_id: "run-1", application_id: "reports", system_id: "research" },
    })

    transport.receive({ id: "run", ok: true, result: { summary: { run_id: "run-1" } } })
    transport.receive({ id: "system", ok: true, result: { summary: { id: "research" } } })

    expect((await system).summary.id).toBe("research")
    expect((await run).summary.run_id).toBe("run-1")
    await client.close()
  })

  test("surfaces structured RPC errors", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, { createID: () => "missing" })
    const request = client.systemDetail("missing")
    await waitForRequests(transport, 1)

    transport.receive({
      id: "missing",
      ok: false,
      error: { code: "not_found", message: "system missing was not found" },
    })

    await expect(request).rejects.toEqual(
      expect.objectContaining({
        name: "BridgeRpcError",
        code: "not_found",
        message: "system missing was not found",
      }),
    )
    await client.close()
  })

  test("delivers assistant stream events without consuming the pending response", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, { createID: () => "turn-1" })
    const events: string[] = []
    client.subscribeEvents((event) => events.push(event.type))

    const turn = client.assistantSend("chat-1", "hello", "powerful")
    await waitForRequests(transport, 1)
    transport.receive({
      event: {
        request_id: "turn-1",
        session_id: "chat-1",
        type: "turn.started",
      },
    })
    transport.receive({
      event: {
        request_id: "turn-1",
        session_id: "chat-1",
        type: "turn.delta",
        text: "hel",
      },
    })
    transport.receive({
      id: "turn-1",
      ok: true,
      result: { session_id: "chat-1", assistant: "hello" },
    })

    expect(JSON.parse(transport.sent[0]!)).toEqual({
      id: "turn-1",
      method: "assistant.send",
      params: { session_id: "chat-1", message: "hello", model_type: "powerful" },
    })
    expect(events).toEqual(["turn.started", "turn.delta"])
    expect((await turn).assistant).toBe("hello")
    await client.close()
  })

  test("surfaces bridge saturation without permanently closing the client", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, { createID: sequentialIDs("busy", "next") })
    const saturated = client.bootstrap()
    await waitForRequests(transport, 1)

    transport.receive({
      id: "busy",
      ok: false,
      error: { code: "busy", message: "bridge request lane is busy; retry shortly" },
    })

    await expect(saturated).rejects.toEqual(
      expect.objectContaining({
        name: "BridgeRpcError",
        code: "busy",
      }),
    )

    const next = client.bootstrap()
    await waitForRequests(transport, 2)
    transport.receive({ id: "next", ok: true, result: { systems: [], runs: [] } })
    expect((await next).systems).toEqual([])
    await client.close()
  })

  test("keeps the bridge usable after runtime and schedule domain errors", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, {
      createID: sequentialIDs("not-ready", "schedule-failed", "next"),
    })

    const runtime = client.request("runtime.summary", {})
    await waitForRequests(transport, 1)
    transport.receive({
      id: "not-ready",
      ok: false,
      error: { code: "not_ready", message: "bootstrap first" },
    })
    await expect(runtime).rejects.toEqual(expect.objectContaining({ code: "not_ready" }))

    const schedule = client.request("schedule.pause", { job_id: "job-1" })
    await waitForRequests(transport, 2)
    transport.receive({
      id: "schedule-failed",
      ok: false,
      error: { code: "schedule_failed", message: "job is busy" },
    })
    await expect(schedule).rejects.toEqual(expect.objectContaining({ code: "schedule_failed" }))

    const next = client.bootstrap()
    await waitForRequests(transport, 3)
    transport.receive({ id: "next", ok: true, result: { systems: [], runs: [] } })
    expect((await next).systems).toEqual([])
    await client.close()
  })

  test("closing rejects pending and future requests", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, { createID: () => "pending" })
    const pending = client.bootstrap()
    await waitForRequests(transport, 1)

    await client.close()

    expect(transport.closed).toBe(true)
    await expect(pending).rejects.toBeInstanceOf(BridgeClosedError)
    await expect(client.bootstrap()).rejects.toBeInstanceOf(BridgeClosedError)
  })

  test("exposes bounded builder and explicit apply RPCs", async () => {
    const transport = new FakeTransport()
    const client = new BridgeClient(transport, { createID: sequentialIDs("send", "draft", "apply") })

    const send = client.builderSend("builder-1", "Create a research system", "powerful")
    const draft = client.builderDraft("builder-1")
    const apply = client.applyDraft("builder-1", 2)
    await waitForRequests(transport, 3)

    expect(transport.sent.map((line) => JSON.parse(line))).toEqual([
      {
        id: "send",
        method: "builder.send",
        params: { session_id: "builder-1", message: "Create a research system", model_type: "powerful" },
      },
      { id: "draft", method: "builder.draft", params: { session_id: "builder-1" } },
      {
        id: "apply",
        method: "draft.apply",
        params: { session_id: "builder-1", expected_revision: 2 },
      },
    ])

    transport.receive({ id: "send", ok: true, result: { session_id: "builder-1" } })
    transport.receive({ id: "draft", ok: true, result: { revision: 2 } })
    transport.receive({ id: "apply", ok: true, result: { applied: true, revision: 2, files: [] } })

    expect((await send).session_id).toBe("builder-1")
    expect((await draft).revision).toBe(2)
    expect((await apply).applied).toBe(true)
    await client.close()
  })
})

function sequentialIDs(...ids: string[]) {
  return () => {
    const id = ids.shift()
    if (!id) throw new Error("test exhausted request ids")
    return id
  }
}

async function waitForRequests(transport: FakeTransport, count: number) {
  while (transport.sent.length < count) await Bun.sleep(0)
}
