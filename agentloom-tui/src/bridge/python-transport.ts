import { existsSync } from "node:fs"
import { resolve } from "node:path"
import { BridgeClosedError, BridgeProtocolError, type BridgeTransport, type BridgeTransportHandlers } from "./client"

// Isolated mode removes the inspected project's cwd and PYTHON* environment
// variables from module resolution, so only the selected environment can
// provide AgentLoom's bridge package.
export const uvPythonBridgeCommand = ["uv", "run", "python", "-I", "-u", "-m", "src.tui_bridge"] as const
export const defaultMaxBridgeLineBytes = 8 * 1024 * 1024

export interface PythonTransportOptions {
  projectRoot?: string
  args?: readonly string[]
  cwd?: string
  command?: readonly string[]
  onStderr?: (chunk: string) => void
  maxLineBytes?: number
}

export function resolveProjectRoot(args: readonly string[], cwd = process.cwd()) {
  const inline = args.find((arg) => arg.startsWith("--project="))
  if (inline) {
    const value = inline.slice("--project=".length)
    if (!value) throw new Error("--project requires a path")
    return resolve(cwd, value)
  }

  const index = args.indexOf("--project")
  if (index === -1) return resolve(cwd)
  const value = args[index + 1]
  if (!value || value.startsWith("--")) throw new Error("--project requires a path")
  return resolve(cwd, value)
}

export function resolvePythonBridgeCommand(
  projectRoot: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
) {
  const installedPython = env.AGENTLOOM_PYTHON?.trim()
  if (installedPython) return [installedPython, "-I", "-u", "-m", "src.tui_bridge"] as const

  const candidates =
    process.platform === "win32"
      ? [resolve(projectRoot, ".venv", "Scripts", "python.exe"), resolve(projectRoot, ".venv", "bin", "python")]
      : [resolve(projectRoot, ".venv", "bin", "python"), resolve(projectRoot, ".venv", "Scripts", "python.exe")]
  const projectPython = candidates.find((candidate) => existsSync(candidate))
  if (projectPython) return [projectPython, "-I", "-u", "-m", "src.tui_bridge"] as const

  return uvPythonBridgeCommand
}

export class PythonTransport implements BridgeTransport {
  readonly projectRoot: string
  readonly command: readonly string[]
  private readonly onStderr: (chunk: string) => void
  private readonly maxLineBytes: number
  private child?: Bun.PipedSubprocess
  private handlers?: BridgeTransportHandlers
  private stdoutTask?: Promise<void>
  private stderrTask?: Promise<void>
  private closed = false
  private failed = false

  constructor(options: PythonTransportOptions = {}) {
    const cwd = options.cwd ?? process.cwd()
    this.projectRoot = options.projectRoot
      ? resolve(cwd, options.projectRoot)
      : resolveProjectRoot(options.args ?? process.argv.slice(2), cwd)
    this.command = options.command ?? resolvePythonBridgeCommand(this.projectRoot)
    this.onStderr = options.onStderr ?? ((chunk) => process.stderr.write(chunk))
    this.maxLineBytes = options.maxLineBytes ?? defaultMaxBridgeLineBytes
    if (!Number.isSafeInteger(this.maxLineBytes) || this.maxLineBytes <= 0) {
      throw new Error("maxLineBytes must be a positive integer")
    }
  }

  async start(handlers: BridgeTransportHandlers) {
    if (this.closed) throw new BridgeClosedError()
    if (this.child) return
    this.handlers = handlers
    this.child = Bun.spawn({
      cmd: [...this.command],
      cwd: this.projectRoot,
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
    })
    this.stdoutTask = this.readStdout(this.child.stdout)
    this.stderrTask = this.readStderr(this.child.stderr)
    void this.child.exited.then(async (code) => {
      // Let stderr reach the caller before reporting a non-zero exit. This keeps
      // the actionable Python diagnostic ahead of the generic exit-code error.
      await this.stderrTask?.catch(() => undefined)
      if (!this.closed && code !== 0) this.fail(new Error(`AgentLoom Python bridge exited with code ${code}`))
      this.handlers?.close()
    })
  }

  async send(line: string) {
    if (this.closed) throw new BridgeClosedError()
    if (!this.child) throw new Error("AgentLoom Python bridge has not started")
    requireJSONObjectLine(line, this.maxLineBytes)
    this.child.stdin.write(`${line}\n`)
    await this.child.stdin.flush()
  }

  async close() {
    if (this.closed) return
    this.closed = true
    const child = this.child
    if (!child) return
    child.stdin.end()
    const exited = await Promise.race([child.exited.then(() => true), Bun.sleep(250).then(() => false)])
    if (!exited) child.kill()
    await child.exited
    await Promise.allSettled([this.stdoutTask, this.stderrTask].filter((task): task is Promise<void> => !!task))
  }

  private async readStdout(stream: ReadableStream<Uint8Array>) {
    const reader = stream.getReader()
    const decoder = new TextDecoder("utf-8", { fatal: true })
    let pending: Uint8Array[] = []
    let pendingBytes = 0
    try {
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        let segmentStart = 0
        for (let index = 0; index < chunk.value.byteLength; index += 1) {
          if (chunk.value[index] !== 0x0a) continue
          const segment = chunk.value.subarray(segmentStart, index)
          if (segment.byteLength > 0) {
            pending.push(segment)
            pendingBytes += segment.byteLength
          }
          if (pendingBytes > this.maxLineBytes) {
            throw new BridgeProtocolError("bridge message exceeds the maximum line length")
          }
          let raw = joinBytes(pending, pendingBytes)
          if (raw.at(-1) === 0x0d) raw = raw.subarray(0, raw.byteLength - 1)
          const line = decoder.decode(raw)
          requireJSONObjectLine(line, this.maxLineBytes)
          this.handlers?.line(line)
          pending = []
          pendingBytes = 0
          segmentStart = index + 1
        }
        const tail = chunk.value.subarray(segmentStart)
        if (tail.byteLength > 0) {
          pending.push(tail)
          pendingBytes += tail.byteLength
        }
        if (pendingBytes > this.maxLineBytes) {
          throw new BridgeProtocolError("bridge message exceeds the maximum line length")
        }
      }
      if (pendingBytes > 0) throw new BridgeProtocolError("bridge stdout ended with an unterminated JSON line")
    } catch (error) {
      this.fail(asError(error))
    } finally {
      reader.releaseLock()
    }
  }

  private async readStderr(stream: ReadableStream<Uint8Array>) {
    const reader = stream.getReader()
    const decoder = new TextDecoder()
    try {
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        const text = decoder.decode(chunk.value, { stream: true })
        if (text) this.onStderr(text)
      }
      const final = decoder.decode()
      if (final) this.onStderr(final)
    } catch (error) {
      if (!this.closed) this.fail(asError(error))
    } finally {
      reader.releaseLock()
    }
  }

  private fail(error: Error) {
    if (this.closed || this.failed) return
    this.failed = true
    this.handlers?.error(error)
    this.child?.kill()
  }
}

function requireJSONObjectLine(line: string, maxLineBytes: number) {
  if (new TextEncoder().encode(line).byteLength > maxLineBytes) {
    throw new BridgeProtocolError("bridge message exceeds the maximum line length")
  }
  if (!line || line.includes("\n") || line.includes("\r")) {
    throw new BridgeProtocolError("bridge messages must contain one JSON object per line")
  }
  const value = (() => {
    try {
      return JSON.parse(line) as unknown
    } catch {
      return undefined
    }
  })()
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new BridgeProtocolError("bridge message must be a JSON object")
  }
}

function joinBytes(chunks: readonly Uint8Array[], byteLength: number) {
  if (chunks.length === 1) return chunks[0]!
  const joined = new Uint8Array(byteLength)
  let offset = 0
  for (const chunk of chunks) {
    joined.set(chunk, offset)
    offset += chunk.byteLength
  }
  return joined
}

function asError(value: unknown) {
  if (value instanceof Error) return value
  return new Error(String(value))
}
