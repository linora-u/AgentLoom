import { afterEach, describe, expect, test } from "bun:test"
import type { ScrollBoxRenderable } from "@opentui/core"
import { testRender } from "@opentui/solid"
import type {
  BootstrapResultDto,
  RpcMethod,
  RpcParams,
  RpcResult,
  SystemDetailResultDto,
} from "../../src/domain"
import { AgentLoomApp } from "../../src/app/view"
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

afterEach(() => {
  for (const renderer of renderers.splice(0)) renderer.destroy()
})

describe("AgentLoom TUI view", () => {
  test("wide terminals show the Builder and the clickable system directory together", async () => {
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
    expect(frame).toContain("Agent Builder")
    expect(frame).toContain("Models: powerful* · fast")
    expect(frame).toContain("Agent Systems")
    expect(frame).toContain("new_agent")
    expect(frame).toContain("/apply")
  })

  test("one wide-sidebar click loads the selected Agent exactly once", async () => {
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

    await setup.mockMouse.click(102, 2)
    await Bun.sleep(50)
    await setup.renderOnce()

    expect(session.state.route).toEqual({
      type: "system",
      systemID: "applications/new/workflows/new.yaml",
    })
    expect(detailCalls).toBe(1)
  })

  test("plain Enter captures and submits the focused Builder input immediately", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "builder.send") {
          messages.push(String((params as RpcParams<"builder.send">).message))
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
    await setup.mockInput.typeText("next draft")
    await Bun.sleep(20)
    await setup.renderOnce()

    expect(messages).toEqual(["what model"])
  })

  test("Shift+Enter inserts a newline without submitting", async () => {
    const messages: string[] = []
    const client: TuiClient = {
      async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>) {
        if (method === "builder.send") {
          messages.push(String((params as RpcParams<"builder.send">).message))
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

  test("mouse wheel scrolls a long sidebar to entries below the viewport", async () => {
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

    expect(setup.captureCharFrame()).toContain("agent_00")
    for (let index = 0; index < 2; index += 1) {
      await setup.mockMouse.scroll(110, 8, "down")
    }
    await setup.renderOnce()

    expect(setup.captureCharFrame()).not.toContain("agent_00")
  })

  test("keyboard selection keeps the selected sidebar entry inside the viewport", async () => {
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

    setup.mockInput.pressTab()
    for (let index = 1; index < longSnapshot.systems.length; index += 1) {
      setup.mockInput.pressArrow("down")
    }
    await setup.renderOnce()

    expect(session.state.selectedIndex).toBe(23)
    expect(setup.renderer.root.findDescendantById("agentloom-sidebar-entry-23")).toBeDefined()
    const sidebar = setup.renderer.root.findDescendantById(
      "agentloom-sidebar-scrollbox",
    ) as ScrollBoxRenderable
    expect(sidebar).toBeDefined()
    expect(sidebar.scrollTop).toBeGreaterThan(0)
    expect(setup.captureCharFrame()).toContain("agent_23")
  })
})
