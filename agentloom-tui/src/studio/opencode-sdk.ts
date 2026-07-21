import { createOpencodeClient } from "@opencode-ai/sdk/v2"
import { realpathSync } from "node:fs"
import { relative, resolve } from "node:path"
import type { OpenCodeSessionInfo } from "./application-sessions"
import type { OpenCodeStoredMessage, OpenCodeStudioApi } from "./opencode-studio"
import type { StudioEvent, StudioModel } from "../app/session"

const MAX_ROOT_SESSIONS = 100_000
const ROOT_SESSION_QUERY_LIMIT = MAX_ROOT_SESSIONS + 1

export function createOpenCodeSessionApi(input: {
  baseUrl: string
  directory: string
  models?: StudioModel[]
}): OpenCodeStudioApi {
  const workspaceKey = canonicalDirectory(input.directory)
  const client = createOpencodeClient({
    baseUrl: input.baseUrl,
    directory: input.directory,
  })
  const listeners = new Set<(event: StudioEvent) => void>()
  const eventAbortController = new AbortController()
  let eventPumpStarted = false
  let eventPump: Promise<void> | null = null

  const subscribe = (listener: (event: StudioEvent) => void) => {
    listeners.add(listener)
    if (!eventPumpStarted) {
      eventPumpStarted = true
      eventPump = pumpEvents(client, input.directory, listeners, eventAbortController.signal)
    }
    return () => listeners.delete(listener)
  }

  return {
    workspaceKey,
    subscribe,
    async close(): Promise<void> {
      eventAbortController.abort()
      await eventPump
      listeners.clear()
    },
    async list(): Promise<OpenCodeSessionInfo[]> {
      const result = await client.session.list({
        directory: input.directory,
        scope: "project",
        roots: true,
        limit: ROOT_SESSION_QUERY_LIMIT,
      })
      if (!result.data) throw sdkError("list OpenCode sessions", result.error)
      if (result.data.length > MAX_ROOT_SESSIONS) {
        throw new Error(
          `OpenCode returned more than ${MAX_ROOT_SESSIONS} root sessions; refusing to use a possibly truncated Application history`,
        )
      }
      return result.data.map(toSessionInfo)
    },

    async create(request): Promise<OpenCodeSessionInfo> {
      const result = await client.session.create({
        directory: input.directory,
        title: request.title,
        metadata: request.metadata,
        permission: request.permission,
      })
      if (!result.data) throw sdkError("create OpenCode session", result.error)
      return toSessionInfo(result.data)
    },

    async update(sessionID, request): Promise<OpenCodeSessionInfo> {
      const result = await client.session.update({
        sessionID,
        directory: input.directory,
        title: request.title,
        metadata: request.metadata,
        permission: request.permission,
      })
      if (!result.data) throw sdkError("update OpenCode session", result.error)
      return toSessionInfo(result.data)
    },

    async messages(sessionID): Promise<OpenCodeStoredMessage[]> {
      const result = await client.session.messages({
        sessionID,
        directory: input.directory,
      })
      if (!result.data) throw sdkError("load OpenCode messages", result.error)
      return result.data.map((message) => ({
        info: {
          id: message.info.id,
          role: message.info.role,
          errorName: message.info.role === "assistant" ? message.info.error?.name : undefined,
        },
        parts: message.parts.map(storedOpenCodePart),
      }))
    },

    async prompt(sessionID, message, system, model): Promise<void> {
      const result = await client.session.prompt({
        sessionID,
        directory: input.directory,
        agent: "build",
        system,
        model,
        parts: [{ type: "text", text: message }],
      })
      if (!result.data) throw sdkError("prompt OpenCode session", result.error)
    },

    async replyPermission(requestID, reply): Promise<void> {
      const result = await client.permission.reply({
        requestID,
        directory: input.directory,
        reply,
      })
      if (!result.data) throw sdkError("reply to OpenCode permission", result.error)
    },

    async replyQuestion(requestID, answers): Promise<void> {
      const result = await client.question.reply({
        requestID,
        directory: input.directory,
        answers,
      })
      if (!result.data) throw sdkError("reply to OpenCode question", result.error)
    },

    async rejectQuestion(requestID): Promise<void> {
      const result = await client.question.reject({
        requestID,
        directory: input.directory,
      })
      if (!result.data) throw sdkError("reject OpenCode question", result.error)
    },

    async abort(sessionID): Promise<void> {
      const result = await client.session.abort({ sessionID, directory: input.directory })
      if (!result.data) throw sdkError("interrupt OpenCode session", result.error)
    },

    async deleteMessage(sessionID, messageID): Promise<void> {
      const result = await client.session.deleteMessage({
        sessionID,
        messageID,
        directory: input.directory,
      })
      if (!result.data) throw sdkError("discard interrupted OpenCode turn", result.error)
    },

    async setPermissions(sessionID, permission): Promise<void> {
      const result = await client.session.update({
        sessionID,
        directory: input.directory,
        permission,
      })
      if (!result.data) throw sdkError("update OpenCode session permissions", result.error)
    },

    async models() {
      if (input.models) return input.models
      const result = await client.provider.list({ directory: input.directory })
      if (!result.data) throw sdkError("list OpenCode models", result.error)
      const connected = new Set(result.data.connected)
      return result.data.all
        .filter((provider) => connected.has(provider.id))
        .flatMap((provider) => Object.values(provider.models).map((model) => ({
          id: `${provider.id}/${model.id}`,
          providerID: provider.id,
          modelID: model.id,
          name: model.name,
          providerName: provider.name,
        })))
        .sort((left, right) => left.providerName.localeCompare(right.providerName)
          || left.name.localeCompare(right.name))
    },
  }
}

async function pumpEvents(
  client: ReturnType<typeof createOpencodeClient>,
  directory: string,
  listeners: Set<(event: StudioEvent) => void>,
  signal: AbortSignal,
): Promise<void> {
  let retryDelayMs = 1_000
  let errorReported = false
  const partTypes = new Map<string, string>()
  while (!signal.aborted) {
    let streamError: unknown
    try {
      const subscription = await client.event.subscribe({ directory }, {
        signal,
        sseMaxRetryAttempts: 1,
        onSseError(error) {
          streamError = error
        },
      })
      for await (const raw of subscription.stream) {
        retryDelayMs = 1_000
        errorReported = false
        const rawEvent = studioEvent(raw, partTypes)
        if (!rawEvent) continue
        const event = normalizeToolEvent(rawEvent, directory)
        const events = [event, ...toolDiffEvents(event, directory)]
        for (const mapped of events) {
          for (const listener of listeners) listener(mapped)
        }
      }
    } catch (error) {
      streamError = error
    }
    if (signal.aborted) return
    if (streamError && !errorReported) {
      errorReported = true
      const event: StudioEvent = { type: "error", message: sdkError("read OpenCode events", streamError).message }
      for (const listener of listeners) listener(event)
    }
    await abortableDelay(retryDelayMs, signal)
    retryDelayMs = Math.min(retryDelayMs * 2, 30_000)
  }
}

function abortableDelay(durationMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve()
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      signal.removeEventListener("abort", finish)
      resolve()
    }
    const timer = setTimeout(finish, durationMs)
    signal.addEventListener("abort", finish, { once: true })
  })
}

function studioEvent(raw: unknown, partTypes: Map<string, string>): StudioEvent | null {
  if (!isRecord(raw) || typeof raw.type !== "string") return null
  // OpenCode 1.18 emits both its legacy in-process event shape
  // (`properties`) and the durable event-log shape (`data`) from /event.
  // The bundled runtime currently uses the latter for Session, Tool and
  // permission progress, so accepting only `properties` makes an active turn
  // look silent and hides the real Session lifecycle from Studio.
  const properties = isRecord(raw.properties)
    ? raw.properties
    : isRecord(raw.data)
      ? raw.data
      : null
  if (!properties) return null
  const sessionID = typeof properties.sessionID === "string" ? properties.sessionID : undefined
  if (raw.type === "message.part.removed" && sessionID && typeof properties.partID === "string") {
    partTypes.delete(studioPartKey(sessionID, properties.partID))
    return null
  }
  if (raw.type === "message.part.updated" && sessionID && isRecord(properties.part)) {
    const part = properties.part
    if (typeof part.id === "string" && typeof part.type === "string") {
      partTypes.set(studioPartKey(sessionID, part.id), part.type)
    }
  }
  if (
    raw.type === "message.part.delta"
    && sessionID
    && typeof properties.partID === "string"
    && partTypes.get(studioPartKey(sessionID, properties.partID)) === "text"
    && properties.field === "text"
    && typeof properties.delta === "string"
  ) {
    return { type: "text.delta", sessionID, text: properties.delta }
  }
  if (raw.type === "message.part.updated" && sessionID && isRecord(properties.part)) {
    const part = properties.part
    if (part.type !== "tool" || typeof part.callID !== "string" || typeof part.tool !== "string" || !isRecord(part.state)) return null
    const status = part.state.status
    if (!isToolStatus(status)) return null
    return {
      type: "tool",
      sessionID,
      callID: part.callID,
      name: part.tool,
      status,
      ...(typeof part.state.title === "string" ? { title: part.state.title } : {}),
      ...(isRecord(part.state.input) ? { input: part.state.input } : {}),
      ...(typeof part.state.output === "string" ? { output: part.state.output } : {}),
      ...(isRecord(part.state.metadata) ? { metadata: part.state.metadata } : {}),
    }
  }
  if (raw.type === "session.diff" && sessionID && Array.isArray(properties.diff)) {
    if (properties.diff.length === 0) return null
    return {
      type: "diff",
      sessionID,
      files: properties.diff.filter(isRecord).map((file) => ({
        ...(typeof file.file === "string" ? { file: file.file } : {}),
        ...(typeof file.patch === "string" ? { patch: file.patch } : {}),
        additions: typeof file.additions === "number" ? file.additions : 0,
        deletions: typeof file.deletions === "number" ? file.deletions : 0,
        ...(typeof file.status === "string" ? { status: file.status } : {}),
      })),
    }
  }
  if (raw.type === "permission.asked" && sessionID && typeof properties.id === "string") {
    return {
      type: "permission",
      sessionID,
      requestID: properties.id,
      permission: typeof properties.permission === "string" ? properties.permission : "unknown",
      patterns: Array.isArray(properties.patterns) ? properties.patterns.filter((item): item is string => typeof item === "string") : [],
      metadata: isRecord(properties.metadata) ? properties.metadata : {},
    }
  }
  if ((raw.type === "session.status" || raw.type === "session.idle") && sessionID) {
    const status = raw.type === "session.idle"
      ? "idle"
      : isRecord(properties.status) && ["busy", "idle", "retry"].includes(String(properties.status.type))
        ? properties.status.type as "busy" | "idle" | "retry"
        : "busy"
    return { type: "status", sessionID, status }
  }
  if (raw.type === "question.asked" && sessionID && typeof properties.id === "string") {
    return {
      type: "question",
      sessionID,
      requestID: properties.id,
      questions: Array.isArray(properties.questions)
        ? properties.questions.flatMap((question) => toStudioQuestion(question))
        : [],
    }
  }
  if (raw.type === "session.error") {
    return { type: "error", ...(sessionID ? { sessionID } : {}), message: errorText(properties.error) }
  }
  return null
}

function studioPartKey(sessionID: string, partID: string): string {
  return `${sessionID}:${partID}`
}

function toolDiffEvents(event: StudioEvent, directory: string): StudioEvent[] {
  if (event.type !== "tool" || event.status !== "completed" || !isRecord(event.metadata?.filediff)) return []
  const file = event.metadata.filediff
  if (typeof file.file !== "string") return []
  return [{
    type: "diff",
    sessionID: event.sessionID,
    files: [{
      file: workspaceRelativePath(directory, file.file),
      ...(typeof file.patch === "string" ? { patch: file.patch } : {}),
      additions: typeof file.additions === "number" ? file.additions : 0,
      deletions: typeof file.deletions === "number" ? file.deletions : 0,
      status: "modified",
    }],
  }]
}

function normalizeToolEvent(event: StudioEvent, directory: string): StudioEvent {
  if (event.type !== "tool" || !isRecord(event.metadata?.filediff)) return event
  const file = event.metadata.filediff
  if (typeof file.file !== "string") return event
  return { ...event, title: workspaceRelativePath(directory, file.file) }
}

function workspaceRelativePath(directory: string, file: string): string {
  const value = relative(canonicalDirectory(directory), canonicalDirectory(file)).replaceAll("\\", "/")
  return value && value !== ".." && !value.startsWith("../") ? value : file
}

function toStudioQuestion(value: unknown) {
  if (!isRecord(value) || typeof value.question !== "string") return []
  const options = Array.isArray(value.options)
    ? value.options.flatMap((option) => (
        isRecord(option) && typeof option.label === "string"
          ? [{
              label: option.label,
              description: typeof option.description === "string" ? option.description : "",
            }]
          : []
      ))
    : []
  return [{
    header: typeof value.header === "string" ? value.header : "Question",
    question: value.question,
    options,
    multiple: value.multiple === true,
    custom: value.custom !== false,
  }]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function isToolStatus(value: unknown): value is "pending" | "running" | "completed" | "error" {
  return value === "pending" || value === "running" || value === "completed" || value === "error"
}

function storedOpenCodePart(part: { type: string; [key: string]: unknown }): OpenCodeStoredMessage["parts"][number] {
  if (part.type === "text" && typeof part.text === "string") {
    return { type: "text", text: part.text }
  }
  if (part.type !== "tool" || typeof part.tool !== "string") return { type: part.type }
  const state = isRecord(part.state) ? part.state : {}
  return {
    type: "tool",
    tool: part.tool,
    state: {
      ...(typeof state.status === "string" ? { status: state.status } : {}),
      ...(typeof state.title === "string" ? { title: state.title } : {}),
      ...(state.input !== undefined ? { input: state.input } : {}),
      ...(typeof state.output === "string" ? { output: state.output } : {}),
      ...(state.error !== undefined ? { error: state.error } : {}),
    },
  }
}

function errorText(value: unknown): string {
  if (isRecord(value) && isRecord(value.data) && typeof value.data.message === "string") return value.data.message
  return typeof value === "string" ? value : "OpenCode session failed"
}

function toSessionInfo(session: {
  id: string
  title: string
  directory: string
  parentID?: string
  time?: { created: number; updated: number }
  metadata?: Record<string, unknown>
}): OpenCodeSessionInfo {
  return {
    id: session.id,
    title: session.title,
    directory: session.directory,
    parentID: session.parentID,
    time: session.time,
    metadata: session.metadata,
  }
}

function sdkError(operation: string, error: unknown): Error {
  const detail = error instanceof Error ? error.message : JSON.stringify(error)
  return new Error(`Failed to ${operation}${detail && detail !== "undefined" ? `: ${detail}` : ""}`)
}

function canonicalDirectory(directory: string): string {
  try {
    return realpathSync(directory)
  } catch {
    return resolve(directory)
  }
}
