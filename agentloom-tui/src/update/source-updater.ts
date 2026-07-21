import { lstat, readdir, readFile } from "node:fs/promises"
import { join, resolve } from "node:path"

export type UpdateCheck = {
  available: boolean
  sourceRoot: string
  installedAt: number
  latestSourceMtime: number
}

export interface UpdateClient {
  check(): Promise<UpdateCheck>
  install(): Promise<void>
}

const SOURCE_ENTRIES = [
  "install",
  "pyproject.toml",
  "uv.lock",
  "src",
  "agentloom-framework-skill",
  "agentloom-tui/package.json",
  "agentloom-tui/bun.lock",
  "agentloom-tui/src",
] as const
const MAX_SOURCE_ENTRIES = 20_000
const MAX_UPDATE_OUTPUT = 64_000

export function createSourceUpdateClient(
  env: Record<string, string | undefined> = process.env,
): UpdateClient | undefined {
  const sourceValue = env.AGENTLOOM_SOURCE_ROOT?.trim()
  const installValue = env.AGENTLOOM_INSTALL_ROOT?.trim()
  const commandValue = env.AGENTLOOM_COMMAND?.trim()
  if (!sourceValue || !installValue || !commandValue) return undefined
  const sourceRoot = resolve(sourceValue)
  const installRoot = resolve(installValue)
  const command = resolve(commandValue)

  return {
    async check() {
      const installedAt = await installedTimestamp(join(installRoot, "installed-at"))
      const latestSourceMtime = await latestSourceTimestamp(sourceRoot)
      return {
        available: latestSourceMtime > installedAt,
        sourceRoot,
        installedAt,
        latestSourceMtime,
      }
    },
    async install() {
      const child = Bun.spawn({
        cmd: [command, "update"],
        cwd: sourceRoot,
        env: process.env,
        stdin: "ignore",
        stdout: "pipe",
        stderr: "pipe",
      })
      const [exitCode, stdout, stderr] = await Promise.all([
        child.exited,
        readBounded(child.stdout),
        readBounded(child.stderr),
      ])
      if (exitCode !== 0) {
        throw new Error(
          `AgentLoom update failed (${exitCode}): ${stderr.trim() || stdout.trim() || "no diagnostic output"}`,
        )
      }
    },
  }
}

async function installedTimestamp(path: string): Promise<number> {
  const raw = (await readFile(path, "utf8")).trim()
  if (!/^\d{1,16}$/.test(raw)) throw new Error("installed-at is missing or invalid")
  const seconds = Number(raw)
  if (!Number.isSafeInteger(seconds) || seconds <= 0) throw new Error("installed-at is missing or invalid")
  return seconds * 1_000
}

async function latestSourceTimestamp(sourceRoot: string): Promise<number> {
  const root = await lstat(sourceRoot)
  if (!root.isDirectory() || root.isSymbolicLink()) throw new Error("trusted source root is unavailable")
  const stack = SOURCE_ENTRIES.map((entry) => join(sourceRoot, entry))
  let latest = 0
  let scanned = 0
  while (stack.length > 0) {
    if (++scanned > MAX_SOURCE_ENTRIES) throw new Error("trusted source update scan exceeded its safe entry limit")
    const path = stack.pop()!
    const stat = await lstat(path).catch(() => undefined)
    if (!stat || stat.isSymbolicLink()) continue
    latest = Math.max(latest, stat.mtimeMs)
    if (!stat.isDirectory()) continue
    const children = await readdir(path, { withFileTypes: true })
    for (const child of children) {
      if (child.isSymbolicLink()) continue
      stack.push(join(path, child.name))
    }
  }
  return latest
}

async function readBounded(stream: ReadableStream<Uint8Array>): Promise<string> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let result = ""
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      result = (result + decoder.decode(next.value, { stream: true })).slice(-MAX_UPDATE_OUTPUT)
    }
    return (result + decoder.decode()).slice(-MAX_UPDATE_OUTPUT)
  } finally {
    reader.releaseLock()
  }
}
