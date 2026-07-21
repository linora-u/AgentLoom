export type OpenCodeRuntimeServer = {
  url: string
  diagnostics(): string
  close(): Promise<void>
}

export type StudioModelRequestParameters = Record<string, {
  temperature?: number
}>

export class OpenCodeRuntime {
  private active: Promise<OpenCodeRuntimeServer> | null = null

  constructor(private readonly input: {
    command: string
    projectRoot: string
    startupTimeoutMs?: number
    /** Additional OpenCode config used by deterministic Runtime integration tests. */
    config?: Record<string, unknown>
    /** Safe per-profile request parameters keyed by generated Provider ID. */
    modelParameters?: StudioModelRequestParameters
    /** Additional Runtime environment used by hermetic integration tests. */
    environment?: Record<string, string | undefined>
    /** Private storage read only through the capability-scoped memory tool. */
    memoryRoot?: string
  }) {}

  start(): Promise<OpenCodeRuntimeServer> {
    if (this.active) return this.active
    this.active = this.startProcess().catch((error) => {
      this.active = null
      throw error
    })
    return this.active
  }

  private async startProcess(): Promise<OpenCodeRuntimeServer> {
    const studioPlugin = await createStudioPlugin(
      this.input.projectRoot,
      this.input.modelParameters,
      this.input.memoryRoot,
    )
    const runtimeConfigPath = join(studioPlugin.directory, "opencode.json")
    await writeFile(runtimeConfigPath, JSON.stringify({
      ...this.input.config,
      autoupdate: false,
      share: "disabled",
      skills: { paths: [join(this.input.projectRoot, "agentloom-framework-skill")] },
      plugin: [pathToFileURL(studioPlugin.path).href],
    }), "utf8")
    await chmod(runtimeConfigPath, 0o600)
    const port = availablePort()
    const process = Bun.spawn({
      cmd: [
        this.input.command,
        "serve",
        "--hostname=127.0.0.1",
        `--port=${port}`,
      ],
      cwd: this.input.projectRoot,
      env: {
        ...Bun.env,
        ...this.input.environment,
        OPENCODE_CONFIG: runtimeConfigPath,
        AGENTLOOM_STUDIO_RUNTIME_CONFIG: runtimeConfigPath,
        OPENCODE_DISABLE_PROJECT_CONFIG: "1",
        OPENCODE_DISABLE_AUTOUPDATE: "1",
      },
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
    })
    const timeoutMs = this.input.startupTimeoutMs ?? 10_000
    const output: string[] = []
    let complete: ((url: string) => void) | undefined
    let fail: ((error: Error) => void) | undefined
    let settled = false
    const listening = new Promise<string>((resolve, reject) => {
      complete = resolve
      fail = reject
    })
    const observe = (text: string) => {
      output.push(text)
      if (output.join("").length > 64_000) output.splice(0, output.length - 16)
      const match = output.join("").match(/opencode server listening on (https?:\/\/[^\s]+)/)
      if (!match || settled) return
      settled = true
      complete?.(match[1]!)
    }
    void consume(process.stdout, observe)
    void consume(process.stderr, observe)
    void process.exited.then((code) => {
      if (settled) return
      settled = true
      fail?.(new Error(`OpenCode Runtime exited during startup (${code}): ${output.join("").trim()}`))
    })
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      process.kill()
      fail?.(new Error(`Timed out waiting for OpenCode Runtime after ${timeoutMs}ms: ${output.join("").trim()}`))
    }, timeoutMs)

    try {
      const url = await listening
      return {
        url,
        diagnostics() {
          return output.join("")
        },
        async close() {
          if (process.exitCode === null) process.kill()
          await process.exited
          await rm(studioPlugin.directory, { recursive: true, force: true })
        },
      }
    } catch (error) {
      if (process.exitCode === null) process.kill()
      await rm(studioPlugin.directory, { recursive: true, force: true })
      throw error
    } finally {
      clearTimeout(timer)
    }
  }
}

async function createStudioPlugin(
  projectRoot: string,
  modelParameters: StudioModelRequestParameters = {},
  memoryRoot?: string,
): Promise<{ directory: string; path: string }> {
  const directory = await mkdtemp(join(tmpdir(), "agentloom-opencode-config-"))
  const path = join(directory, "agentloom-plugin.js")
  await writeFile(path, domainToolSource(projectRoot, modelParameters, memoryRoot), "utf8")
  return { directory, path }
}

export function domainToolSource(
  projectRoot: string,
  modelParameters: StudioModelRequestParameters = {},
  memoryRoot = studioMemoryRoot(projectRoot),
): string {
  return `import { lstatSync, readFileSync, readdirSync, unlinkSync } from "node:fs"
import { join } from "node:path"

const RUNTIME_CONFIG_PATH = process.env.AGENTLOOM_STUDIO_RUNTIME_CONFIG
if (RUNTIME_CONFIG_PATH) {
  try { unlinkSync(RUNTIME_CONFIG_PATH) } catch (error) {
    if (!error || typeof error !== "object" || error.code !== "ENOENT") throw error
  }
}
delete process.env.AGENTLOOM_STUDIO_RUNTIME_CONFIG
delete process.env.OPENCODE_CONFIG
delete process.env.OPENCODE_CONFIG_CONTENT

const MAX_DOMAIN_OUTPUT_BYTES = 24 * 1024
const MAX_MEMORY_CHUNK_CHARS = 128 * 1024
const STUDIO_MODEL_PARAMETERS = ${JSON.stringify(modelParameters)}
const STUDIO_MEMORY_ROOT = ${JSON.stringify(memoryRoot)}

const latestMemoryGeneration = (capability) => {
  if (!STUDIO_MEMORY_ROOT || !/^[a-f0-9]{64}$/.test(capability)) {
    throw new Error("Invalid AgentLoom memory capability")
  }
  const applicationDirectory = join(STUDIO_MEMORY_ROOT, capability)
  let generations
  try {
    const rootInfo = lstatSync(STUDIO_MEMORY_ROOT)
    const applicationInfo = lstatSync(applicationDirectory)
    if (
      !rootInfo.isDirectory()
      || rootInfo.isSymbolicLink()
      || !applicationInfo.isDirectory()
      || applicationInfo.isSymbolicLink()
    ) throw new Error("unsafe memory directory")
    generations = readdirSync(applicationDirectory, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
      .map((entry) => entry.name)
      .sort()
      .reverse()
  } catch {
    throw new Error("No persisted Application memory is available")
  }
  for (const generation of generations) {
    const directory = join(applicationDirectory, generation)
    const indexPath = join(directory, "index.json")
    try {
      const info = lstatSync(indexPath)
      if (!info.isFile() || info.isSymbolicLink()) continue
      const index = JSON.parse(readFileSync(indexPath, "utf8"))
      if (!index || ![1, 2].includes(index.schema_version) || !Array.isArray(index.conversations)) continue
      return { directory, index }
    } catch {}
  }
  throw new Error("No complete Application memory generation is available")
}

const boundedInteger = (value, fallback, maximum) => {
  if (!Number.isFinite(value)) return fallback
  return Math.max(0, Math.min(maximum, Math.floor(value)))
}

export const AgentLoomPlugin = async () => ({
  "chat.params": async (input, output) => {
    const parameters = STUDIO_MODEL_PARAMETERS[input.model.providerID]
    if (!parameters) return
    if (typeof parameters.temperature === "number") output.temperature = parameters.temperature
  },
  tool: {
  agentloom_domain: {
  description: "Inspect, validate, run, and diagnose AgentLoom Applications through the versioned Python domain contract.",
  args: {
    action: {
      type: "string",
      enum: ["catalog", "application.detail", "application.validate", "application.impact", "run.start", "run.stop", "run.resume", "run.restart", "run.detail"],
      description: "AgentLoom domain operation"
    },
    params: {
      type: "object",
      description: "Operation parameters such as application_id, run_id, yaml_path, task, or task_id"
    }
  },
  async execute(args, context) {
    if (args.action.startsWith("run.")) {
      const applicationID = args.params && typeof args.params.application_id === "string"
        ? args.params.application_id
        : "unknown"
      const pattern = args.action + ":" + applicationID
      await context.ask({
        permission: "agentloom_run",
        patterns: [pattern],
        always: [pattern],
        metadata: { action: args.action, application_id: applicationID }
      })
    }
    const python = process.env.AGENTLOOM_PYTHON || ${JSON.stringify(join(projectRoot, ".venv/bin/python"))}
    if (context.abort && context.abort.aborted) {
      throw new Error("AgentLoom domain operation aborted before launch")
    }
    const child = Bun.spawn({
      cmd: [python, "-I", "-m", "src.tui_bridge.domain_cli", "--project", context.directory, args.action, JSON.stringify(args.params || {})],
      cwd: context.directory,
      env: process.env,
      detached: process.platform !== "win32",
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe"
    })
    let forceKillTimer
    const killProcessGroup = (signal) => {
      try {
        if (process.platform === "win32") child.kill(signal)
        else process.kill(-child.pid, signal)
      } catch {
        try { child.kill(signal) } catch {}
      }
    }
    const terminate = () => {
      killProcessGroup("SIGTERM")
      forceKillTimer = setTimeout(() => killProcessGroup("SIGKILL"), 3_000)
    }
    context.abort && context.abort.addEventListener("abort", terminate, { once: true })
    let exitCode
    let stdout
    let stderr
    try {
      ;[exitCode, stdout, stderr] = await Promise.all([
        child.exited,
        new Response(child.stdout).text(),
        new Response(child.stderr).text()
      ])
    } finally {
      context.abort && context.abort.removeEventListener("abort", terminate)
      if (forceKillTimer) clearTimeout(forceKillTimer)
    }
    if (context.abort && context.abort.aborted) {
      throw new Error("AgentLoom domain operation aborted")
    }
    if (exitCode !== 0) throw new Error(stderr.trim() || stdout.trim() || "AgentLoom domain operation failed")
    const rawOutput = stdout.trim()
    const outputBytes = new TextEncoder().encode(rawOutput).byteLength
    const output = outputBytes <= MAX_DOMAIN_OUTPUT_BYTES
      ? rawOutput
      : JSON.stringify({
          contract_version: 1,
          ok: false,
          error: {
            code: "output_too_large",
            message: "AgentLoom domain output exceeded 24 KiB. Use the action's offset/limit parameters or request a narrower entity."
          }
        })
    return {
      title: "AgentLoom " + args.action,
      output,
      metadata: { action: args.action, outputBytes, bounded: outputBytes > MAX_DOMAIN_OUTPUT_BYTES }
    }
  }
  },
  agentloom_memory: {
    description: "List or read persisted conversations for the current Application using the capability supplied by the Studio system prompt.",
    args: {
      action: { type: "string", enum: ["list", "read"] },
      capability: { type: "string", description: "Opaque capability from the Studio system prompt" },
      session_id: { type: "string", description: "Conversation ID required for read" },
      offset: { type: "number", description: "Character offset for pagination" },
      limit: { type: "number", description: "Maximum conversations for list or characters for read" }
    },
    async execute(args) {
      const memory = latestMemoryGeneration(args.capability)
      if (args.action === "list") {
        const offset = boundedInteger(args.offset, 0, memory.index.conversations.length)
        const limit = boundedInteger(args.limit, 50, 200)
        const conversations = memory.index.conversations.slice(offset, offset + limit)
          .map(({ session_id, updated_at }) => ({ session_id, updated_at }))
        return {
          title: "AgentLoom Application memory",
          output: JSON.stringify({
            application_id: memory.index.application_id,
            total: memory.index.conversations.length,
            offset,
            conversations
          }),
          metadata: { action: "list", count: conversations.length }
        }
      }
      if (args.action !== "read" || typeof args.session_id !== "string") {
        throw new Error("agentloom_memory read requires session_id")
      }
      const entry = memory.index.conversations.find((item) => item.session_id === args.session_id)
      if (!entry) {
        throw new Error("Conversation is not available through this capability")
      }
      const readChunk = (file) => {
        if (typeof file !== "string" || file.includes("/") || file.includes("\\\\")) {
          throw new Error("Unsafe Application memory transcript")
        }
        const transcriptPath = join(memory.directory, file)
        const info = lstatSync(transcriptPath)
        if (!info.isFile() || info.isSymbolicLink()) throw new Error("Unsafe Application memory transcript")
        return readFileSync(transcriptPath, "utf8")
      }
      const limit = Math.max(1, boundedInteger(args.limit, 24 * 1024, 24 * 1024))
      let totalChars
      let offset
      let output
      if (Array.isArray(entry.chunks)) {
        if (entry.chunks.length === 0) throw new Error("Invalid Application memory chunk index")
        const chunks = entry.chunks.map((chunk) => {
          if (
            !chunk
            || typeof chunk !== "object"
            || typeof chunk.file !== "string"
            || chunk.file.includes("/")
            || chunk.file.includes("\\\\")
            || !Number.isSafeInteger(chunk.chars)
            || chunk.chars < 0
            || chunk.chars > MAX_MEMORY_CHUNK_CHARS
          ) throw new Error("Invalid Application memory chunk index")
          return chunk
        })
        totalChars = chunks.reduce((total, chunk) => total + chunk.chars, 0)
        if (!Number.isSafeInteger(totalChars)) throw new Error("Invalid Application memory length")
        offset = boundedInteger(args.offset, 0, totalChars)
        let cursor = 0
        output = ""
        for (const chunk of chunks) {
          const chunkEnd = cursor + chunk.chars
          if (offset < chunkEnd && output.length < limit) {
            const content = readChunk(chunk.file)
            if (content.length !== chunk.chars) throw new Error("Application memory chunk is incomplete")
            const start = Math.max(0, offset - cursor)
            output += content.slice(start, start + limit - output.length)
          }
          cursor = chunkEnd
          if (output.length >= limit) break
        }
      } else if (typeof entry.file === "string") {
        const transcript = readChunk(entry.file)
        totalChars = transcript.length
        offset = boundedInteger(args.offset, 0, totalChars)
        output = transcript.slice(offset, offset + limit)
      } else {
        throw new Error("Conversation is not available through this capability")
      }
      return {
        title: "AgentLoom conversation " + args.session_id,
        output,
        metadata: {
          action: "read",
          session_id: args.session_id,
          offset,
          next_offset: offset + output.length < totalChars ? offset + output.length : null,
          total_chars: totalChars
        }
      }
    }
  }
  } })
`
}

function availablePort(): number {
  const reservation = Bun.serve({
    port: 0,
    fetch: () => new Response(null, { status: 503 }),
  })
  const port = reservation.port
  reservation.stop(true)
  if (typeof port !== "number") throw new Error("Failed to reserve a local port for OpenCode Runtime")
  return port
}

async function consume(
  stream: ReadableStream<Uint8Array>,
  observe: (text: string) => void,
): Promise<void> {
  const decoder = new TextDecoder()
  const reader = stream.getReader()
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      observe(decoder.decode(next.value, { stream: true }))
    }
    const tail = decoder.decode()
    if (tail) observe(tail)
  } finally {
    reader.releaseLock()
  }
}
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"
import { studioMemoryRoot } from "./application-memory"
