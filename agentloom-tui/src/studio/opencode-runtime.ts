export type OpenCodeRuntimeServer = {
  url: string
  diagnostics(): string
  close(): Promise<void>
}

export class OpenCodeRuntime {
  private active: Promise<OpenCodeRuntimeServer> | null = null

  constructor(private readonly input: {
    command: string
    projectRoot: string
    startupTimeoutMs?: number
    /** Additional OpenCode config used by deterministic Runtime integration tests. */
    config?: Record<string, unknown>
    /** Additional Runtime environment used by hermetic integration tests. */
    environment?: Record<string, string | undefined>
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
    const studioPlugin = await createStudioPlugin(this.input.projectRoot)
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
        OPENCODE_CONFIG_CONTENT: JSON.stringify({
          ...this.input.config,
          autoupdate: false,
          share: "disabled",
          skills: { paths: [join(this.input.projectRoot, "agentloom-framework-skill")] },
          plugin: [pathToFileURL(studioPlugin.path).href],
        }),
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

async function createStudioPlugin(projectRoot: string): Promise<{ directory: string; path: string }> {
  const directory = await mkdtemp(join(tmpdir(), "agentloom-opencode-config-"))
  const path = join(directory, "agentloom-plugin.js")
  await writeFile(path, domainToolSource(projectRoot), "utf8")
  return { directory, path }
}

export function domainToolSource(projectRoot: string): string {
  return `const MAX_DOMAIN_OUTPUT_BYTES = 24 * 1024

export const AgentLoomPlugin = async () => ({ tool: { agentloom_domain: {
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
} } })
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
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"
