import { afterEach, describe, expect, test } from "bun:test"
import { createOpencodeClient } from "@opencode-ai/sdk/v2"
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { ApplicationStudioSessions } from "../../src/studio/application-sessions"
import { domainToolSource, OpenCodeRuntime } from "../../src/studio/opencode-runtime"
import { createOpenCodeSessionApi } from "../../src/studio/opencode-sdk"
import { OpenCodeStudioClient } from "../../src/studio/opencode-studio"
import { startOpenCodeStudio } from "../../src/studio/start"

const cleanups: Array<() => Promise<void> | void> = []

afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup()
})

describe("bundled OpenCode Runtime", () => {
  test("rejects oversized domain output before OpenCode can persist it as a managed file", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-bounded-output-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const fakePython = join(projectRoot, "fake-python")
    await writeFile(
      fakePython,
      "#!/bin/sh\ndd if=/dev/zero bs=70000 count=1 2>/dev/null | tr '\\000' x\n",
      "utf8",
    )
    await chmod(fakePython, 0o755)
    const pluginPath = join(projectRoot, "agentloom-plugin.mjs")
    await writeFile(pluginPath, domainToolSource(projectRoot), "utf8")
    const pluginModule = await import(`${pluginPath}?test=${crypto.randomUUID()}`)
    const plugin = await pluginModule.AgentLoomPlugin()
    const previousPython = process.env.AGENTLOOM_PYTHON
    process.env.AGENTLOOM_PYTHON = fakePython
    try {
      const result = await plugin.tool.agentloom_domain.execute(
        { action: "catalog", params: {} },
        { directory: projectRoot, async ask() {} },
      )

      expect(result.output.length).toBeLessThan(4_096)
      expect(result.output).toContain("output_too_large")
    } finally {
      if (previousPython === undefined) delete process.env.AGENTLOOM_PYTHON
      else process.env.AGENTLOOM_PYTHON = previousPython
    }
  })

  test("run domain actions ask OpenCode permission before starting Python", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-permission-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const pluginPath = join(projectRoot, "agentloom-plugin.mjs")
    await writeFile(pluginPath, domainToolSource(projectRoot), "utf8")
    const pluginModule = await import(`${pluginPath}?test=${crypto.randomUUID()}`)
    const plugin = await pluginModule.AgentLoomPlugin()
    const marker = new Error("permission boundary reached")
    let request: unknown

    await expect(plugin.tool.agentloom_domain.execute(
      { action: "run.start", params: { application_id: "reports" } },
      {
        directory: projectRoot,
        async ask(value: unknown) {
          request = value
          throw marker
        },
      },
    )).rejects.toBe(marker)
    expect(request).toEqual({
      permission: "agentloom_run",
      patterns: ["run.start:reports"],
      always: ["run.start:reports"],
      metadata: { action: "run.start", application_id: "reports" },
    })
  })

  test("aborting an OpenCode tool kills the whole Python process group", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-abort-domain-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const fakePython = join(projectRoot, "fake-python")
    const parentMarker = join(projectRoot, "parent.pid")
    const childMarker = join(projectRoot, "child.pid")
    await writeFile(
      fakePython,
      [
        "#!/bin/sh",
        "echo $$ > \"$AGENTLOOM_TEST_PARENT_PID\"",
        "sleep 300 &",
        "echo $! > \"$AGENTLOOM_TEST_CHILD_PID\"",
        "wait",
      ].join("\n") + "\n",
      "utf8",
    )
    await chmod(fakePython, 0o755)
    const pluginPath = join(projectRoot, "agentloom-plugin.mjs")
    await writeFile(pluginPath, domainToolSource(projectRoot), "utf8")
    const pluginModule = await import(`${pluginPath}?test=${crypto.randomUUID()}`)
    const plugin = await pluginModule.AgentLoomPlugin()
    const controller = new AbortController()
    const previous = {
      python: process.env.AGENTLOOM_PYTHON,
      parent: process.env.AGENTLOOM_TEST_PARENT_PID,
      child: process.env.AGENTLOOM_TEST_CHILD_PID,
    }
    process.env.AGENTLOOM_PYTHON = fakePython
    process.env.AGENTLOOM_TEST_PARENT_PID = parentMarker
    process.env.AGENTLOOM_TEST_CHILD_PID = childMarker
    try {
      const execution = plugin.tool.agentloom_domain.execute(
        { action: "catalog", params: {} },
        { directory: projectRoot, abort: controller.signal, async ask() {} },
      )
      await waitFor(async () => Bun.file(childMarker).exists())
      const parentPID = Number((await readFile(parentMarker, "utf8")).trim())
      const childPID = Number((await readFile(childMarker, "utf8")).trim())

      controller.abort()

      await expect(execution).rejects.toThrow("aborted")
      await waitFor(() => !processExists(parentPID) && !processExists(childPID))
    } finally {
      restoreEnv("AGENTLOOM_PYTHON", previous.python)
      restoreEnv("AGENTLOOM_TEST_PARENT_PID", previous.parent)
      restoreEnv("AGENTLOOM_TEST_CHILD_PID", previous.child)
    }
  }, 10_000)

  test("registers the AgentLoom domain tool in the real Runtime", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-tool-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const runtime = new OpenCodeRuntime({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
    })
    const server = await runtime.start()
    cleanups.push(() => server.close())
    const client = createOpencodeClient({ baseUrl: server.url, directory: projectRoot })

    const tools = await client.tool.ids({ directory: projectRoot })

    expect(tools.error).toBeUndefined()
    expect(tools.data).toContain("agentloom_domain")
  }, 20_000)

  test("serves persistent Application sessions through the real SDK", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-runtime-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const runtime = new OpenCodeRuntime({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
    })
    const server = await runtime.start()
    cleanups.push(() => server.close())
    const sessions = new ApplicationStudioSessions(createOpenCodeSessionApi({
      baseUrl: server.url,
      directory: projectRoot,
    }))

    const created = await sessions.open("runtime-smoke")
    const resumed = await sessions.open("runtime-smoke")

    expect(created.id).toStartWith("ses_")
    expect(resumed.id).toBe(created.id)
  }, 20_000)

  test("runs a deterministic LLM tool loop through the real OpenCode Runtime and Python domain", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-llm-e2e-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const workflowDirectory = join(projectRoot, "applications/runtime_demo/workflows")
    await mkdir(workflowDirectory, { recursive: true })
    await writeFile(join(workflowDirectory, "demo.yaml"), [
      "name: runtime_demo_agent",
      "description: Deterministic OpenCode Runtime integration fixture",
      "model_type: powerful",
      "tool_call_type: code_act",
      "workflow: Validate the Application without changing files.",
      "tools: []",
      "worker_agents: []",
      "skills: []",
      "execution_env:",
      "  type: local",
    ].join("\n") + "\n", "utf8")
    const llm = new DeterministicLlmServer("runtime_demo")
    cleanups.push(() => llm.close())
    const runtime = new OpenCodeRuntime({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
      environment: {
        AGENTLOOM_PYTHON: resolve(import.meta.dir, "../../../.venv/bin/python"),
      },
      config: deterministicProviderConfig(llm.url),
    })
    const server = await runtime.start()
    cleanups.push(() => server.close())
    const studio = new OpenCodeStudioClient(createOpenCodeSessionApi({
      baseUrl: server.url,
      directory: projectRoot,
    }))
    cleanups.push(() => studio.close())
    const events: Array<{ type: string; [key: string]: unknown }> = []
    studio.subscribe((event) => events.push(event))

    const opened = await studio.openApplication("runtime_demo")
    const models = await studio.listModels()
    expect(models.map((model) => model.id)).toContain("test/test-model")
    await studio.setModel(opened.sessionID, "test/test-model")

    let result
    try {
      result = await studio.send(opened.sessionID, "请真实校验当前 Application，并报告结果。")
    } catch (error) {
      throw new Error(`${String(error)}\nLLM requests: ${JSON.stringify(llm.debugRequests())}\nRuntime: ${server.diagnostics()}`)
    }

    expect(result.messages.some((message) => (
      message.role === "assistant"
      && message.content === "已通过真实 OpenCode 工具链完成校验：runtime_demo 有效。"
    ))).toBeTrue()
    expect(llm.requests.some((body) => requestHasTool(body, "agentloom_domain"))).toBeTrue()
    expect(llm.requests.some((body) => requestHasDomainResult(body, "runtime_demo"))).toBeTrue()
    expect(events.some((event) => (
      event.type === "tool"
      && event.name === "agentloom_domain"
      && event.status === "completed"
    ))).toBeTrue()
  }, 30_000)

  test("pauses a real OpenCode LLM turn for a question and resumes after the Studio answer", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-question-e2e-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const llm = new DeterministicQuestionServer()
    cleanups.push(() => llm.close())
    const runtime = new OpenCodeRuntime({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
      config: deterministicProviderConfig(llm.url),
    })
    const server = await runtime.start()
    cleanups.push(() => server.close())
    const studio = new OpenCodeStudioClient(createOpenCodeSessionApi({
      baseUrl: server.url,
      directory: projectRoot,
    }))
    cleanups.push(() => studio.close())
    const events: Array<{ type: string; [key: string]: unknown }> = []
    studio.subscribe((event) => events.push(event))
    const opened = await studio.openApplication("question_demo")
    await studio.setModel(opened.sessionID, "test/test-model")

    const sending = studio.send(opened.sessionID, "拓扑不明确时先问我，再继续。")
    await waitFor(() => events.some((event) => event.type === "question"))
    const question = events.find((event) => event.type === "question")
    expect(question?.questions).toEqual([{
      header: "Topology",
      question: "选择 Application 拓扑",
      options: [
        { label: "Supervisor + Workers", description: "适合需要分工的流程" },
        { label: "Single Agent", description: "适合简单流程" },
      ],
      multiple: false,
      custom: true,
    }])
    await studio.replyQuestion(String(question?.requestID), [["Supervisor + Workers"]])

    const result = await sending
    expect(result.messages.some((message) => (
      message.role === "assistant"
      && message.content === "已收到选择：Supervisor + Workers。"
    ))).toBeTrue()
    expect(llm.requests.some((body) => requestHasQuestionAnswer(body, "Supervisor + Workers"))).toBeTrue()
  }, 30_000)

  test("rejecting a real OpenCode run permission prevents the Python operation from starting", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-permission-e2e-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const marker = join(projectRoot, "python-started")
    const fakePython = join(projectRoot, "fake-python")
    await writeFile(fakePython, `#!/bin/sh\necho started > ${JSON.stringify(marker)}\nexit 91\n`, "utf8")
    await chmod(fakePython, 0o755)
    const llm = new DeterministicPermissionServer()
    cleanups.push(() => llm.close())
    const runtime = new OpenCodeRuntime({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
      environment: { AGENTLOOM_PYTHON: fakePython },
      config: deterministicProviderConfig(llm.url),
    })
    const server = await runtime.start()
    cleanups.push(() => server.close())
    const studio = new OpenCodeStudioClient(createOpenCodeSessionApi({
      baseUrl: server.url,
      directory: projectRoot,
    }))
    cleanups.push(() => studio.close())
    const events: Array<{ type: string; [key: string]: unknown }> = []
    studio.subscribe((event) => events.push(event))
    const opened = await studio.openApplication("permission_demo")
    await studio.setModel(opened.sessionID, "test/test-model")

    const sending = studio.send(opened.sessionID, "运行前必须让我确认。")
    await waitFor(() => events.some((event) => event.type === "permission"))
    const permission = events.find((event) => event.type === "permission")
    expect(permission).toMatchObject({
      permission: "agentloom_run",
      patterns: ["run.start:permission_demo"],
    })
    await studio.replyPermission(String(permission?.requestID), "reject")
    await sending

    expect(await Bun.file(marker).exists()).toBeFalse()
    expect(events.some((event) => (
      event.type === "tool"
      && event.name === "agentloom_domain"
      && event.status === "error"
    ))).toBeTrue()
  }, 30_000)

  test("edits the selected Application through a real OpenCode tool call and emits its Diff", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-edit-e2e-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const workflowDirectory = join(projectRoot, "applications/edit_demo/workflows")
    const workflowPath = join(workflowDirectory, "demo.yaml")
    await mkdir(workflowDirectory, { recursive: true })
    await writeFile(workflowPath, [
      "name: edit_demo_agent",
      "description: Before the Studio edit",
      "workflow: Answer clearly.",
      "worker_agents: []",
    ].join("\n") + "\n", "utf8")
    const llm = new DeterministicEditServer("applications/edit_demo/workflows/demo.yaml")
    cleanups.push(() => llm.close())
    const runtime = new OpenCodeRuntime({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
      config: deterministicProviderConfig(llm.url),
    })
    const server = await runtime.start()
    cleanups.push(() => server.close())
    const studio = new OpenCodeStudioClient(createOpenCodeSessionApi({
      baseUrl: server.url,
      directory: projectRoot,
    }))
    cleanups.push(() => studio.close())
    const events: Array<{ type: string; [key: string]: unknown }> = []
    studio.subscribe((event) => events.push(event))
    const opened = await studio.openApplication("edit_demo")
    await studio.setModel(opened.sessionID, "test/test-model")

    const result = await Promise.race([
      studio.send(opened.sessionID, "把 description 改成 After the Studio edit。"),
      Bun.sleep(12_000).then(() => {
        throw new Error(`Edit turn timed out\nEvents: ${JSON.stringify(events)}\nRequests: ${JSON.stringify(llm.debugRequests())}\nRuntime: ${server.diagnostics()}`)
      }),
    ])

    expect(await readFile(workflowPath, "utf8")).toContain("description: After the Studio edit")
    expect(result.messages.some((message) => (
      message.role === "assistant"
      && message.content === "已修改当前 Application，并保留 Diff。"
    ))).toBeTrue()
    expect(events.some((event) => (
      event.type === "tool"
      && event.name === "edit"
      && event.status === "completed"
      && event.title === "applications/edit_demo/workflows/demo.yaml"
    ))).toBeTrue()
    const emittedDiff = events.some((event) => (
      event.type === "diff"
      && Array.isArray(event.files)
      && event.files.some((file) => (
        file !== null
        && typeof file === "object"
        && String((file as Record<string, unknown>).file).endsWith("applications/edit_demo/workflows/demo.yaml")
      ))
    ))
    if (!emittedDiff) throw new Error(`Missing edited file in session.diff events: ${JSON.stringify(events)}`)
  }, 30_000)

  test("production Studio composition owns the Runtime lifecycle", async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), "agentloom-opencode-composition-"))
    cleanups.push(() => rm(projectRoot, { recursive: true, force: true }))
    const studio = await startOpenCodeStudio({
      command: resolve(import.meta.dir, "../../node_modules/.bin/opencode"),
      projectRoot,
      startupTimeoutMs: 15_000,
    })
    cleanups.push(() => studio.close())

    const opened = await studio.client.openApplication("composition-smoke")

    expect(opened.sessionID).toStartWith("ses_")
  }, 20_000)
})

async function waitFor(check: () => boolean | Promise<boolean>, timeoutMs = 5_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await check()) return
    await Bun.sleep(20)
  }
  throw new Error(`Condition did not become true within ${timeoutMs}ms`)
}

function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function restoreEnv(key: string, value: string | undefined): void {
  if (value === undefined) delete process.env[key]
  else process.env[key] = value
}

function deterministicProviderConfig(baseURL: string): Record<string, unknown> {
  return {
    formatter: false,
    lsp: false,
    model: "test/test-model",
    small_model: "test/test-model",
    provider: {
      test: {
        name: "Test",
        id: "test",
        env: [],
        npm: "@ai-sdk/openai-compatible",
        models: {
          "test-model": {
            id: "test-model",
            name: "Test Model",
            attachment: false,
            reasoning: false,
            temperature: false,
            tool_call: true,
            release_date: "2025-01-01",
            limit: { context: 100_000, output: 10_000 },
            cost: { input: 0, output: 0 },
            options: {},
          },
        },
        options: { apiKey: "test-key", baseURL },
      },
    },
  }
}

class DeterministicLlmServer {
  readonly requests: Record<string, unknown>[] = []
  private readonly server: ReturnType<typeof Bun.serve>

  constructor(private readonly applicationID: string) {
    this.server = Bun.serve({
      port: 0,
      fetch: async (request) => {
        if (request.method !== "POST") return new Response("not found", { status: 404 })
        const body = await request.json() as Record<string, unknown>
        this.requests.push(body)
        const serialized = JSON.stringify(body)
        if (serialized.includes("Generate a title for this conversation")) {
          return chatCompletionText("Runtime E2E")
        }
        if (requestHasDomainResult(body, this.applicationID)) {
          return chatCompletionText(`已通过真实 OpenCode 工具链完成校验：${this.applicationID} 有效。`)
        }
        return chatCompletionTool("agentloom_domain", {
          action: "application.validate",
          params: { application_id: this.applicationID },
        })
      },
    })
  }

  get url(): string {
    return `http://127.0.0.1:${this.server.port}/v1`
  }

  close(): void {
    this.server.stop(true)
  }

  debugRequests(): unknown[] {
    return this.requests.map((body) => ({
      roles: Array.isArray(body.messages)
        ? body.messages.map((message) => (
            message && typeof message === "object" && "role" in message
              ? (message as { role?: unknown }).role
              : "unknown"
          ))
        : [],
      hasDomainResult: requestHasDomainResult(body, this.applicationID),
      hasDomainTool: requestHasTool(body, "agentloom_domain"),
    }))
  }
}

class DeterministicQuestionServer {
  readonly requests: Record<string, unknown>[] = []
  private readonly server: ReturnType<typeof Bun.serve>

  constructor() {
    this.server = Bun.serve({
      port: 0,
      fetch: async (request) => {
        if (request.method !== "POST") return new Response("not found", { status: 404 })
        const body = await request.json() as Record<string, unknown>
        this.requests.push(body)
        if (JSON.stringify(body).includes("Generate a title for this conversation")) {
          return chatCompletionText("Question E2E")
        }
        if (requestHasQuestionAnswer(body, "Supervisor + Workers")) {
          return chatCompletionText("已收到选择：Supervisor + Workers。")
        }
        return chatCompletionTool("question", {
          questions: [{
            header: "Topology",
            question: "选择 Application 拓扑",
            options: [
              { label: "Supervisor + Workers", description: "适合需要分工的流程" },
              { label: "Single Agent", description: "适合简单流程" },
            ],
            multiple: false,
            custom: true,
          }],
        })
      },
    })
  }

  get url(): string {
    return `http://127.0.0.1:${this.server.port}/v1`
  }

  close(): void {
    this.server.stop(true)
  }
}

class DeterministicPermissionServer {
  readonly requests: Record<string, unknown>[] = []
  private readonly server: ReturnType<typeof Bun.serve>
  private turnRequests = 0

  constructor() {
    this.server = Bun.serve({
      port: 0,
      fetch: async (request) => {
        if (request.method !== "POST") return new Response("not found", { status: 404 })
        const body = await request.json() as Record<string, unknown>
        this.requests.push(body)
        if (JSON.stringify(body).includes("Generate a title for this conversation")) {
          return chatCompletionText("Permission E2E")
        }
        this.turnRequests += 1
        if (this.turnRequests > 1) return chatCompletionText("已遵循用户的拒绝决定。")
        return chatCompletionTool("agentloom_domain", {
          action: "run.start",
          params: { application_id: "permission_demo", task: "smoke" },
        })
      },
    })
  }

  get url(): string {
    return `http://127.0.0.1:${this.server.port}/v1`
  }

  close(): void {
    this.server.stop(true)
  }
}

class DeterministicEditServer {
  readonly requests: Record<string, unknown>[] = []
  private readonly server: ReturnType<typeof Bun.serve>

  constructor(private readonly workflowPath: string) {
    this.server = Bun.serve({
      port: 0,
      fetch: async (request) => {
        if (request.method !== "POST") return new Response("not found", { status: 404 })
        const body = await request.json() as Record<string, unknown>
        this.requests.push(body)
        const serialized = JSON.stringify(body)
        if (serialized.includes("Generate a title for this conversation")) {
          return chatCompletionText("Edit E2E")
        }
        if (serialized.includes("Edit applied successfully")) {
          return chatCompletionText("已修改当前 Application，并保留 Diff。")
        }
        return chatCompletionTool("edit", {
          filePath: this.workflowPath,
          oldString: "description: Before the Studio edit",
          newString: "description: After the Studio edit",
        })
      },
    })
  }

  get url(): string {
    return `http://127.0.0.1:${this.server.port}/v1`
  }

  close(): void {
    this.server.stop(true)
  }

  debugRequests(): unknown[] {
    return this.requests.map((body) => ({
      roles: Array.isArray(body.messages)
        ? body.messages.map((message) => (
            message && typeof message === "object" && "role" in message
              ? (message as { role?: unknown }).role
              : "unknown"
          ))
        : [],
      hasEditResult: JSON.stringify(body).includes("Edit applied successfully"),
      hasEditTool: requestHasTool(body, "edit"),
    }))
  }
}

function requestHasTool(body: Record<string, unknown>, name: string): boolean {
  return JSON.stringify(body.tools ?? []).includes(`\"name\":\"${name}\"`)
}

function requestHasDomainResult(body: Record<string, unknown>, applicationID: string): boolean {
  const messages = Array.isArray(body.messages) ? body.messages : []
  return messages.some((message) => {
    if (!message || typeof message !== "object") return false
    const value = message as Record<string, unknown>
    const serialized = JSON.stringify(value)
    return value.role === "tool"
      && serialized.includes("contract_version")
      && serialized.includes(applicationID)
  })
}

function requestHasQuestionAnswer(body: Record<string, unknown>, answer: string): boolean {
  const messages = Array.isArray(body.messages) ? body.messages : []
  return messages.some((message) => (
    message !== null
    && typeof message === "object"
    && (message as Record<string, unknown>).role === "tool"
    && JSON.stringify(message).includes("User has answered your questions")
    && JSON.stringify(message).includes(answer)
  ))
}

function chatCompletionText(content: string): Response {
  return sseResponse([
    chatChunk({ role: "assistant" }),
    chatChunk({ content }),
    chatChunk({}, "stop"),
  ])
}

function chatCompletionTool(name: string, input: unknown): Response {
  return sseResponse([
    chatChunk({ role: "assistant" }),
    chatChunk({
      tool_calls: [{
        index: 0,
        id: "call_agentloom_domain",
        type: "function",
        function: { name, arguments: JSON.stringify(input) },
      }],
    }),
    chatChunk({}, "tool_calls"),
  ])
}

function chatChunk(delta: Record<string, unknown>, finishReason: string | null = null): Record<string, unknown> {
  return {
    id: "chatcmpl-agentloom-e2e",
    object: "chat.completion.chunk",
    created: 1,
    model: "test-model",
    choices: [{ index: 0, delta, finish_reason: finishReason }],
    ...(finishReason ? {
      usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    } : {}),
  }
}

function sseResponse(chunks: Record<string, unknown>[]): Response {
  const payload = [...chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`), "data: [DONE]\n\n"].join("")
  return new Response(payload, {
    headers: { "content-type": "text/event-stream" },
  })
}
