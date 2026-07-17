import { afterEach, describe, expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import type {
  BootstrapResultDto,
  RpcMethod,
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
})
