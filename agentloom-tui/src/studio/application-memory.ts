import { createHash, createHmac, randomBytes } from "node:crypto"
import {
  chmod,
  lstat,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join, relative, resolve } from "node:path"
import { setTimeout as delay } from "node:timers/promises"
import type { OpenCodeSessionInfo } from "./application-sessions"
import type { OpenCodeStoredMessage } from "./opencode-studio"

const COMPLETE_GENERATIONS_TO_KEEP = 3
const MAX_TRANSCRIPT_CHUNK_CHARS = 128 * 1024

export type ApplicationMemoryLocation = {
  capability: string
}

export interface ApplicationMemorySource {
  workspaceKey?: string
  listApplicationSessions(applicationID: string): Promise<OpenCodeSessionInfo[]>
  messages(sessionID: string): Promise<OpenCodeStoredMessage[]>
}

type MemoryEntry = {
  session_id: string
  updated_at: number
  total_chars: number
  chunks: Array<{ file: string; chars: number }>
}

export function studioMemoryRoot(workspace: string): string {
  const workspaceID = createHash("sha256").update(workspace).digest("hex")
  return join(homedir(), ".agentloom", "state", "studio-memory", workspaceID)
}

export class ApplicationMemoryArchive {
  private readonly root: string | null
  private readonly rootAnchor: string | null

  constructor(source: ApplicationMemorySource, root?: string | null) {
    this.source = source
    const defaultRoot = source.workspaceKey ? studioMemoryRoot(source.workspaceKey) : null
    this.root = root === null
      ? null
      : root ?? defaultRoot
    this.rootAnchor = this.root
      ? defaultRoot && resolve(this.root) === resolve(defaultRoot)
        ? homedir()
        : dirname(resolve(this.root))
      : null
  }

  private readonly source: ApplicationMemorySource

  async sync(applicationID: string): Promise<ApplicationMemoryLocation | null> {
    if (!this.root || !this.rootAnchor) return null
    const root = await privateDirectory(this.root, this.rootAnchor)
    const secret = await capabilitySecret(root)
    const capability = createHmac("sha256", secret).update(applicationID).digest("hex")
    const applicationDirectory = await privateDirectory(join(root, capability), root)
    const sessions = (await this.source.listApplicationSessions(applicationID))
      .toSorted((left, right) => sessionTimestamp(right) - sessionTimestamp(left))
    const freshness = sessions.reduce(
      (latest, session) => Math.max(latest, sessionTimestamp(session)),
      0,
    )
    const generation = [
      String(freshness).padStart(16, "0"),
      String(Date.now()).padStart(13, "0"),
      crypto.randomUUID(),
    ].join("-")
    const generationDirectory = join(applicationDirectory, generation)
    await privateDirectory(generationDirectory, applicationDirectory)

    try {
      const entries: MemoryEntry[] = []
      for (const session of sessions) {
        const messages = await this.source.messages(session.id)
        const content = transcript(applicationID, session, messages)
        const prefix = Buffer.from(session.id, "utf8").toString("base64url")
        const chunks = transcriptChunks(content).map((chunk, index) => ({
          content: chunk,
          file: `${prefix}.${String(index).padStart(6, "0")}.md`,
        }))
        for (const chunk of chunks) {
          await privateFile(join(generationDirectory, chunk.file), chunk.content)
        }
        entries.push({
          session_id: session.id,
          updated_at: sessionTimestamp(session),
          total_chars: content.length,
          chunks: chunks.map((chunk) => ({ file: chunk.file, chars: chunk.content.length })),
        })
      }
      await privateFile(join(generationDirectory, "index.json"), JSON.stringify({
        schema_version: 2,
        application_id: applicationID,
        generated_at: Date.now(),
        conversations: entries,
      }))
    } catch (error) {
      await rm(generationDirectory, { recursive: true, force: true })
      throw error
    }

    await removeOldCompleteGenerations(applicationDirectory)
    return { capability }
  }
}

function transcript(
  applicationID: string,
  session: OpenCodeSessionInfo,
  messages: OpenCodeStoredMessage[],
): string {
  const lines = [
    `# ${applicationID} conversation ${session.id}`,
    "",
    `Updated: ${formatTimestamp(sessionTimestamp(session))}`,
    "",
  ]
  for (const message of messages) {
    lines.push(`## ${message.info.role === "user" ? "User" : "Assistant"}${
      message.info.errorName ? ` (${message.info.errorName})` : ""
    }`, "")
    for (const part of message.parts) {
      if (part.type === "text" && typeof part.text === "string") {
        const text = redactSensitive(part.text.trim())
        if (text) lines.push(text, "")
        continue
      }
      const tool = toolMemory(part)
      if (tool) lines.push(tool, "")
    }
  }
  return `${lines.join("\n")}\n`
}

function transcriptChunks(content: string): string[] {
  const chunks: string[] = []
  let offset = 0
  while (offset < content.length) {
    let end = Math.min(content.length, offset + MAX_TRANSCRIPT_CHUNK_CHARS)
    if (
      end < content.length
      && end > offset
      && content.charCodeAt(end - 1) >= 0xD800
      && content.charCodeAt(end - 1) <= 0xDBFF
    ) end -= 1
    chunks.push(content.slice(offset, end))
    offset = end
  }
  return chunks.length > 0 ? chunks : [""]
}

function toolMemory(part: OpenCodeStoredMessage["parts"][number]): string | null {
  if (part.type !== "tool" || typeof part.tool !== "string") return null
  const state = isRecord(part.state) ? part.state : {}
  const status = typeof state.status === "string" ? ` · ${state.status}` : ""
  const title = typeof state.title === "string" ? ` · ${redactSensitive(state.title)}` : ""
  const lines = [`### Tool: ${part.tool}${status}${title}`]
  if (state.input !== undefined) {
    lines.push("", "Input:", "```json", redactSensitive(safeJson(state.input)), "```")
  }
  if (typeof state.output === "string" && state.output.trim()) {
    lines.push("", "Output:", "```text", redactSensitive(state.output.trim()), "```")
  }
  if (state.error !== undefined) {
    const error = typeof state.error === "string" ? state.error : safeJson(state.error)
    lines.push("", "Error:", "```text", redactSensitive(error.trim()), "```")
  }
  return lines.join("\n")
}

function redactSensitive(value: string): string {
  return value
    .replace(/(authorization\s*:\s*)(?:bearer\s+)?\S+/gi, "$1[REDACTED]")
    .replace(
      /((?:"|')?(?:(?:api|access|refresh|id|auth)[_-]?token|(?:api|secret|private|access|session)[_-]?key|client[_-]?secret|secret|credentials?|authorization|password|passwd|pwd|cookie|database[_-]?url|db[_-]?url)(?:"|')?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      "$1[REDACTED]",
    )
    .replace(/(--(?:token|api[_-]?key|password|secret)(?:=|\s+))\S+/gi, "$1[REDACTED]")
    .replace(/\b([A-Z][A-Z0-9_]{2,}\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/g, "$1[REDACTED]")
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^/\s:@]+:[^@\s/]+@/gi, "$1[REDACTED]@")
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[REDACTED]")
    .replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, "[REDACTED]")
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[REDACTED]")
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return "[unserializable tool input]"
  }
}

function sessionTimestamp(session: OpenCodeSessionInfo): number {
  return session.time?.updated ?? session.time?.created ?? 0
}

function formatTimestamp(timestamp: number): string {
  return timestamp > 0 ? new Date(timestamp).toISOString() : "unknown"
}

async function privateDirectory(path: string, trustedAncestor: string): Promise<string> {
  const absolute = resolve(path)
  const anchor = resolve(trustedAncestor)
  const belowAnchor = relative(anchor, absolute)
  if (belowAnchor === ".." || belowAnchor.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`)) {
    throw new Error(`Studio memory directory escapes its trusted ancestor: ${path}`)
  }
  const anchorInfo = await lstat(anchor)
  if (!anchorInfo.isDirectory() || anchorInfo.isSymbolicLink()) {
    throw new Error(`Unsafe Studio memory ancestor: ${anchor}`)
  }
  let current = anchor
  for (const component of belowAnchor.split(/[\\/]+/).filter(Boolean)) {
    current = join(current, component)
    try {
      await mkdir(current, { mode: 0o700 })
    } catch (error) {
      if (!isNodeError(error) || error.code !== "EEXIST") throw error
    }
    const info = await lstat(current)
    if (!info.isDirectory() || info.isSymbolicLink()) {
      throw new Error(`Unsafe Studio memory directory: ${current}`)
    }
    await chmod(current, 0o700)
  }
  return absolute
}

async function privateFile(path: string, content: string): Promise<void> {
  await writeFile(path, content, { encoding: "utf8", flag: "wx", mode: 0o600 })
  await chmod(path, 0o600)
}

async function capabilitySecret(root: string): Promise<Buffer> {
  const path = join(root, "capability-secret")
  let created = false
  try {
    await writeFile(path, randomBytes(32).toString("hex"), { flag: "wx", mode: 0o600 })
    created = true
  } catch (error) {
    if (!isNodeError(error) || error.code !== "EEXIST") throw error
  }
  for (let attempt = 0; attempt < (created ? 1 : 20); attempt += 1) {
    const info = await lstat(path)
    if (!info.isFile() || info.isSymbolicLink()) throw new Error(`Unsafe Studio memory secret: ${path}`)
    await chmod(path, 0o600)
    const value = (await readFile(path, "utf8")).trim()
    if (/^[a-f0-9]{64}$/.test(value)) return Buffer.from(value, "hex")
    if (!created && attempt < 19) await delay(5)
  }
  throw new Error("Invalid Studio memory capability secret")
}

async function removeOldCompleteGenerations(applicationDirectory: string): Promise<void> {
  const generations = (await readdir(applicationDirectory, { withFileTypes: true }))
    .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
    .map((entry) => entry.name)
    .toSorted()
    .toReversed()
  let complete = 0
  for (const generation of generations) {
    const directory = join(applicationDirectory, generation)
    const index = await lstat(join(directory, "index.json")).catch(() => null)
    if (!index?.isFile() || index.isSymbolicLink()) continue
    complete += 1
    if (complete > COMPLETE_GENERATIONS_TO_KEEP) {
      await rm(directory, { recursive: true, force: true })
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

function isNodeError(value: unknown): value is NodeJS.ErrnoException {
  return value instanceof Error && "code" in value
}
