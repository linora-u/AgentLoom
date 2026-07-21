import type { BuilderMessage, StudioClient, StudioEvent, StudioModel } from "../app/session"
import {
  ApplicationStudioSessions,
  applicationOnlyPermissions,
  newApplicationPermissions,
  type OpenCodePermissionRule,
  type OpenCodeSessionApi,
} from "./application-sessions"

export type OpenCodeStoredMessage = {
  info: {
    id: string
    role: "user" | "assistant"
    errorName?: string
  }
  parts: Array<
    | { type: "text"; text: string }
    | { type: string; [key: string]: unknown }
  >
}

export interface OpenCodeStudioApi extends OpenCodeSessionApi {
  close(): Promise<void>
  messages(sessionID: string): Promise<OpenCodeStoredMessage[]>
  prompt(
    sessionID: string,
    message: string,
    system?: string,
    model?: { providerID: string; modelID: string },
  ): Promise<void>
  subscribe(listener: (event: StudioEvent) => void): () => void
  replyPermission(requestID: string, reply: "once" | "always" | "reject"): Promise<void>
  replyQuestion(requestID: string, answers: string[][]): Promise<void>
  rejectQuestion(requestID: string): Promise<void>
  abort(sessionID: string): Promise<void>
  deleteMessage(sessionID: string, messageID: string): Promise<void>
  setPermissions(sessionID: string, permission: OpenCodePermissionRule[]): Promise<void>
  models(): Promise<StudioModel[]>
}

type ActiveStudioTurn = {
  timer: ReturnType<typeof setTimeout> | null
  children: Set<string>
  baselineMessageIDs: Set<string>
  reject(error: Error): void
}

const DEFAULT_STALL_TIMEOUT_MS = 60_000

export class OpenCodeStudioClient implements StudioClient {
  private readonly sessions: ApplicationStudioSessions
  private readonly applicationBySession = new Map<string, string>()
  private readonly listeners = new Set<(event: StudioEvent) => void>()
  private readonly modelsBySession = new Map<string, StudioModel>()
  private readonly unsubscribeApi: () => void
  private readonly activeTurns = new Map<string, ActiveStudioTurn>()
  private readonly parentByChildSession = new Map<string, string>()
  private readonly parentByRequest = new Map<string, string>()
  private readonly knownMessageIDsBySession = new Map<string, Set<string>>()
  private readonly stallTimeoutMs: number

  constructor(
    private readonly api: OpenCodeStudioApi,
    options: { stallTimeoutMs?: number } = {},
  ) {
    this.sessions = new ApplicationStudioSessions(api)
    this.stallTimeoutMs = Math.max(1, options.stallTimeoutMs ?? DEFAULT_STALL_TIMEOUT_MS)
    this.unsubscribeApi = this.api.subscribe((event) => {
      this.observeTurnProgress(event)
      for (const listener of this.listeners) listener(event)
    })
  }

  async close(): Promise<void> {
    this.unsubscribeApi()
    for (const turn of this.activeTurns.values()) {
      if (turn.timer) clearTimeout(turn.timer)
    }
    this.activeTurns.clear()
    this.parentByChildSession.clear()
    this.parentByRequest.clear()
    this.listeners.clear()
    await this.api.close()
  }

  async openApplication(applicationID: string) {
    const session = await this.sessions.open(applicationID)
    this.applicationBySession.set(session.id, applicationID)
    const messages = await this.loadCleanHistory(session.id)
    return {
      sessionID: session.id,
      messages: mapMessages(messages),
    }
  }

  async openNewApplication() {
    const session = await this.sessions.openNew()
    this.applicationBySession.set(session.id, "__new__")
    const messages = await this.loadCleanHistory(session.id)
    return {
      sessionID: session.id,
      messages: mapMessages(messages),
    }
  }

  async send(sessionID: string, message: string) {
    const applicationID = this.applicationBySession.get(sessionID)
    if (!applicationID) throw new Error("Open the Application before sending a Studio message")
    const selected = this.modelsBySession.get(sessionID)
    const baselineMessageIDs = new Set(this.knownMessageIDsBySession.get(sessionID) ?? [])
    await this.withStallWatchdog(sessionID, baselineMessageIDs, () => this.api.prompt(
        sessionID,
        message,
        studioSystemPrompt(applicationID),
        selected ? { providerID: selected.providerID, modelID: selected.modelID } : undefined,
      ))
    const messages = await this.api.messages(sessionID)
    this.rememberMessages(sessionID, messages)
    return {
      messages: mapMessages(messages),
    }
  }

  subscribe(listener: (event: StudioEvent) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async replyPermission(requestID: string, reply: "once" | "always" | "reject"): Promise<void> {
    const parent = this.parentByRequest.get(requestID)
    try {
      await this.api.replyPermission(requestID, reply)
    } finally {
      this.parentByRequest.delete(requestID)
      if (parent) this.armStallWatchdog(parent)
    }
  }

  async replyQuestion(requestID: string, answers: string[][]): Promise<void> {
    const parent = this.parentByRequest.get(requestID)
    try {
      await this.api.replyQuestion(requestID, answers)
    } finally {
      this.parentByRequest.delete(requestID)
      if (parent) this.armStallWatchdog(parent)
    }
  }

  async rejectQuestion(requestID: string): Promise<void> {
    const parent = this.parentByRequest.get(requestID)
    try {
      await this.api.rejectQuestion(requestID)
    } finally {
      this.parentByRequest.delete(requestID)
      if (parent) this.armStallWatchdog(parent)
    }
  }

  async interrupt(sessionID: string): Promise<void> {
    const turn = this.activeTurns.get(sessionID)
    if (!turn) return
    await this.abortAndDiscardTurn(sessionID, turn)
    turn?.reject(new Error("Studio Agent 已中止当前回合。"))
  }

  setPermissionMode(
    sessionID: string,
    mode: "application_only" | "full_access",
  ): Promise<void> {
    const applicationID = this.applicationBySession.get(sessionID)
    if (!applicationID) throw new Error("Open the Application before changing Studio permissions")
    const permission = mode === "full_access"
      ? [{ permission: "*", pattern: "*", action: "allow" as const }]
      : applicationID === "__new__"
        ? newApplicationPermissions()
        : applicationOnlyPermissions(applicationID, this.api.workspaceKey)
    return this.api.setPermissions(sessionID, permission)
  }

  listModels(): Promise<StudioModel[]> {
    return this.api.models()
  }

  async setModel(sessionID: string, modelID: string): Promise<void> {
    if (!this.applicationBySession.has(sessionID)) {
      throw new Error("Open the Application before changing the Studio model")
    }
    const model = (await this.api.models()).find((candidate) => candidate.id === modelID)
    if (!model) throw new Error(`Unknown OpenCode model: ${modelID}`)
    this.modelsBySession.set(sessionID, model)
  }

  private withStallWatchdog<T>(
    sessionID: string,
    baselineMessageIDs: Set<string>,
    operation: () => Promise<T>,
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      let settled = false
      const finish = (complete: () => void) => {
        if (settled) return
        settled = true
        this.cleanupTurn(sessionID, turn)
        complete()
      }
      const turn: ActiveStudioTurn = {
        timer: null,
        children: new Set(),
        baselineMessageIDs,
        reject: (error) => finish(() => reject(error)),
      }
      this.activeTurns.set(sessionID, turn)
      this.armStallWatchdog(sessionID)
      void operation().then(
        (value) => finish(() => resolve(value)),
        (error) => finish(() => reject(error)),
      )
    })
  }

  private observeTurnProgress(event: StudioEvent): void {
    let parent = this.parentByChildSession.get(event.sessionID ?? "") ?? event.sessionID
    if (event.type === "tool" && event.name === "task") {
      const child = typeof event.metadata?.sessionId === "string"
        ? event.metadata.sessionId
        : typeof event.metadata?.sessionID === "string"
          ? event.metadata.sessionID
          : null
      if (child && parent) {
        if (event.status === "running" || event.status === "pending") {
          this.parentByChildSession.set(child, parent)
          this.activeTurns.get(parent)?.children.add(child)
        } else {
          this.parentByChildSession.delete(child)
          this.activeTurns.get(parent)?.children.delete(child)
        }
      }
    }
    if (!parent) return
    const turn = this.activeTurns.get(parent)
    if (!turn) return
    if (event.type === "permission" || event.type === "question") {
      this.parentByRequest.set(event.requestID, parent)
      if (turn.timer) clearTimeout(turn.timer)
      turn.timer = null
      return
    }
    this.armStallWatchdog(parent)
    if (event.sessionID !== parent && event.type === "status" && event.status === "idle") {
      this.parentByChildSession.delete(event.sessionID)
      turn.children.delete(event.sessionID)
    }
  }

  private armStallWatchdog(sessionID: string): void {
    const turn = this.activeTurns.get(sessionID)
    if (!turn) return
    if (turn.timer) clearTimeout(turn.timer)
    turn.timer = setTimeout(() => {
      turn.timer = null
      void this.abortAndDiscardTurn(sessionID, turn).finally(() => {
        turn.reject(new Error(
          `Studio Agent 连续 ${Math.ceil(this.stallTimeoutMs / 1_000)} 秒无进展，已自动中止；请缩小问题或重试。`,
        ))
      })
    }, this.stallTimeoutMs)
  }

  private async abortAndDiscardTurn(sessionID: string, turn: ActiveStudioTurn): Promise<void> {
    const children = [...turn.children]
    await Promise.allSettled(children.map((child) => this.api.abort(child)))
    await this.api.abort(sessionID)

    // OpenCode's abort intentionally preserves an unfinished turn. That is
    // useful for /undo, but it lets the next autonomous loop resume stale tool
    // context. Studio treats Esc as a hard task boundary: retain any file
    // changes already made, while removing only messages created by this turn.
    const messages = await this.api.messages(sessionID)
    const created = messages.filter((item) => !turn.baselineMessageIDs.has(item.info.id))
    for (const item of created.toReversed()) {
      await this.api.deleteMessage(sessionID, item.info.id)
    }
    this.knownMessageIDsBySession.set(sessionID, new Set(turn.baselineMessageIDs))
  }

  private async loadCleanHistory(sessionID: string): Promise<OpenCodeStoredMessage[]> {
    let messages = await this.api.messages(sessionID)
    const deleteIDs = new Set<string>()
    for (let index = 0; index < messages.length; index += 1) {
      const message = messages[index]!
      if (message.info.role !== "assistant" || message.info.errorName !== "MessageAbortedError") continue
      let turnStart = index
      for (let previous = index - 1; previous >= 0; previous -= 1) {
        if (messages[previous]!.info.role !== "user") continue
        turnStart = previous
        break
      }
      for (let remove = turnStart; remove <= index; remove += 1) {
        deleteIDs.add(messages[remove]!.info.id)
      }
    }
    if (deleteIDs.size > 0) {
      for (const message of messages.toReversed()) {
        if (deleteIDs.has(message.info.id)) await this.api.deleteMessage(sessionID, message.info.id)
      }
      messages = messages.filter((message) => !deleteIDs.has(message.info.id))
    }
    this.rememberMessages(sessionID, messages)
    return messages
  }

  private rememberMessages(sessionID: string, messages: OpenCodeStoredMessage[]): void {
    this.knownMessageIDsBySession.set(sessionID, new Set(messages.map((message) => message.info.id)))
  }

  private cleanupTurn(sessionID: string, turn: ActiveStudioTurn): void {
    if (this.activeTurns.get(sessionID) !== turn) return
    if (turn.timer) clearTimeout(turn.timer)
    this.activeTurns.delete(sessionID)
    for (const child of turn.children) {
      if (this.parentByChildSession.get(child) === sessionID) {
        this.parentByChildSession.delete(child)
      }
    }
    for (const [requestID, parent] of this.parentByRequest) {
      if (parent === sessionID) this.parentByRequest.delete(requestID)
    }
  }
}

export function studioSystemPrompt(applicationID: string): string {
  if (applicationID === "__new__") return newApplicationSystemPrompt()
  const applicationPath = `applications/${applicationID}`
  return [
    "你是 AgentLoom Application Studio 的独立控制面 Agent，不是被管理的 Python Application。",
    `当前唯一目标 Application 是 ${applicationID}，可写范围是 ${applicationPath}；可读取整个 AgentLoom 项目以获取事实。`,
    "需要 AgentLoom 配置、Effective Config、拓扑、Skills、权限、校验或 Run 真相时，先加载 agentloom-framework-skill，并调用 agentloom_domain 专业工具；不要从自由文本日志猜测状态。application.detail 默认返回有界分页，更多 Agent 使用 offset/limit 继续读取。",
    "不要读取 OpenCode managed tool-output 的完整文件，也不要为了概览问题启动 Explore 子 Agent；优先使用 agentloom_domain 的结构化摘要和分页。",
    "回合边界遵循 OpenCode Session：最新一条用户消息是当前唯一任务；MessageAbortedError 表示上一回合已经终止。除非最新消息明确要求，否则不得恢复或继续被中止的旧任务。",
    "默认自治 Loop：理解需求 → 检查事实 → 修改 → 静态校验 → 冒烟运行 → 读取结构化证据 → 自动修复，直到满足验收标准。",
    "当前 Application 内的普通创建和编辑直接完成，并保留 OpenCode Diff；不要引入独立草稿写入门槛。",
    "只有缺失业务意图会实质改变结果，或涉及删除、跨 Application/全局写入、扩大权限、首次运行控制或不可逆外部副作用时才暂停询问。",
    "运行中的 Application 固定使用启动时 Revision；文件修改只在显式新 Run、重启或合法恢复边界生效。",
    "完成前必须验证 YAML/Schema、引用、Effective Config、Tools、Skills、权限、拓扑和相关测试；行为变化需在获准后真实冒烟运行。",
    "若未获真实运行授权，只能报告“配置已验证，尚未运行”并列出风险，不能宣称完全完成。",
    "不得读取或输出 Application 模型密钥、Base URL、认证 Headers 或 Provider 私密配置。",
  ].join("\n")
}

function newApplicationSystemPrompt(): string {
  return [
    "你是 AgentLoom Application Studio 的独立控制面 Agent，不是被管理的 Python Application。",
    "当前任务是创建一个新的 AgentLoom Application。先从用户请求提取目标、输入、输出和验收标准；只有缺失业务意图会导致明显不同实现时才询问。",
    "可读取整个项目获取事实。创建目录必须位于 applications/<new-id>，不得修改任何已有 Application；所有写入都由权限请求明确授权。",
    "先加载 agentloom-framework-skill，并使用 agentloom_domain 获取配置语义、校验和运行真相。",
    "回合边界遵循 OpenCode Session：最新一条用户消息是当前唯一任务；MessageAbortedError 表示上一回合已经终止。除非最新消息明确要求，否则不得恢复或继续被中止的旧任务。",
    "自治 Loop：理解需求 → 检查事实 → 创建配置 → 静态校验 → 获准后冒烟运行 → 读取结构化证据 → 自动修复。",
    "直接创建文件并保留 OpenCode Diff，不使用独立草稿或 /apply。完成前必须验证 YAML/Schema、引用、Effective Config、Tools、Skills、权限和拓扑。",
    "若未获真实运行授权，只能报告“配置已验证，尚未运行”，不能宣称完全完成。",
    "不得读取或输出模型密钥、Base URL、认证 Headers 或 Provider 私密配置。",
  ].join("\n")
}

function mapMessages(messages: OpenCodeStoredMessage[]): BuilderMessage[] {
  return messages.flatMap((message) => {
    const content = message.parts
      .filter((part): part is { type: "text"; text: string } => (
        part.type === "text" && typeof part.text === "string"
      ))
      .map((part) => part.text)
      .join("\n")
      .trim()
    if (!content) return []
    return [{
      id: message.info.id,
      role: message.info.role,
      content,
    }]
  })
}
