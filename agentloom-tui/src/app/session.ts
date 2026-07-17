import type {
  BootstrapResultDto,
  RpcMethod,
  RpcParams,
  RpcResult,
  RunDetailResultDto,
  RunSummaryDto,
  SystemDetailResultDto,
  SystemSummaryDto,
} from "../domain"
import {
  buildSidebarGroups,
  nextSelection,
  parseBuilderInput,
  routeForEntry,
  type AppRoute,
  type SidebarEntry,
} from "./controller"

export interface TuiClient {
  request<Method extends RpcMethod>(
    method: Method,
    params: RpcParams<Method>,
  ): Promise<RpcResult<Method>>
  close(): Promise<void> | void
}

export type BuilderDraftFile = {
  path: string
  change: string
  content: string
}

export type BuilderDraft = {
  revision: number
  valid: boolean
  errors: string[]
  files: BuilderDraftFile[]
}

export type BuilderMessage = {
  id: number
  role: "user" | "assistant"
  content: string
}

export type AgentLoomSessionState = {
  snapshot: BootstrapResultDto
  route: AppRoute
  selectedIndex: number
  sidebarOpen: boolean
  modelType: string | null
  messages: BuilderMessage[]
  draft: BuilderDraft | null
  systemDetail: SystemDetailResultDto | null
  runDetail: RunDetailResultDto | null
  busy: boolean
  notice: string | null
}

type Listener = (state: AgentLoomSessionState) => void

export class AgentLoomSession {
  private current: AgentLoomSessionState
  private readonly listeners = new Set<Listener>()
  private refreshPromise: Promise<void> | null = null
  private liveRefreshPromise: Promise<void> | null = null
  private routeLoadGeneration = 0
  private builderBusy = false
  private routeBusy = false
  private messageID = 0

  constructor(
    private readonly input: {
      client: TuiClient
      snapshot: BootstrapResultDto
      sessionID?: string
    },
  ) {
    this.sessionID = input.sessionID ?? crypto.randomUUID()
    this.current = {
      snapshot: input.snapshot,
      route: { type: "builder" },
      selectedIndex: 0,
      sidebarOpen: false,
      modelType: input.snapshot.models.default,
      messages: [
        {
          id: this.nextMessageID(),
          role: "assistant",
          content: "描述你要创建或修改的 Agent。草稿校验通过后，输入 /apply 才会写入项目。",
        },
      ],
      draft: null,
      systemDetail: null,
      runDetail: null,
      busy: false,
      notice: null,
    }
  }

  readonly sessionID: string

  get state(): AgentLoomSessionState {
    return this.current
  }

  get entries(): SidebarEntry[] {
    const groups = buildSidebarGroups(this.current.snapshot)
    return [...groups.systems, ...groups.runs]
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  select(delta: number): void {
    this.patch({
      selectedIndex: nextSelection(this.current.selectedIndex, delta, this.entries.length),
    })
  }

  setSelected(index: number): void {
    if (index < 0 || index >= this.entries.length) return
    this.patch({ selectedIndex: index })
  }

  setSidebarOpen(open: boolean): void {
    this.patch({ sidebarOpen: open })
  }

  goBuilder(): void {
    if (this.current.route.type === "builder") {
      if (!this.builderBusy) this.patch({ notice: null })
      return
    }
    this.routeLoadGeneration += 1
    this.routeBusy = false
    this.patch({
      route: { type: "builder" },
      systemDetail: null,
      runDetail: null,
      notice: null,
    })
  }

  async openSelected(): Promise<void> {
    const entry = this.entries[this.current.selectedIndex]
    if (!entry) return
    await this.openEntry(entry)
  }

  async openEntry(entry: SidebarEntry): Promise<void> {
    const index = this.entries.findIndex((candidate) => candidate.key === entry.key)
    this.routeBusy = true
    this.patch({
      route: routeForEntry(entry),
      selectedIndex: index < 0 ? this.current.selectedIndex : index,
      sidebarOpen: false,
      systemDetail: null,
      runDetail: null,
      notice: null,
    })
    await this.loadCurrentRoute()
  }

  refresh(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise
    this.refreshPromise = this.performRefresh().finally(() => {
      this.refreshPromise = null
    })
    return this.refreshPromise
  }

  refreshLive(): Promise<void> {
    if (this.current.route.type !== "run") return Promise.resolve()
    if (this.liveRefreshPromise) return this.liveRefreshPromise
    this.liveRefreshPromise = this.loadCurrentRoute().finally(() => {
      this.liveRefreshPromise = null
    })
    return this.liveRefreshPromise
  }

  async submit(raw: string): Promise<void> {
    if (this.builderBusy) return
    const command = parseBuilderInput(raw)
    if (command.type === "empty") return
    if (command.type === "models") {
      this.patch({
        messages: [
          ...this.current.messages,
          {
            id: this.nextMessageID(),
            role: "assistant",
            content: modelCatalogMessage(this.current.snapshot),
          },
        ],
        notice: null,
      })
      return
    }
    if (command.type === "model") {
      const model = this.current.snapshot.models.items.find((item) => item.type === command.modelType)
      if (!model?.configured) {
        this.patch({ notice: `模型不可用: ${command.modelType}` })
        return
      }
      this.patch({ modelType: model.type, notice: `已切换模型: ${model.type}` })
      return
    }
    if (command.type === "refresh") {
      await this.refresh()
      return
    }
    if (command.type === "apply") {
      await this.applyDraft()
      return
    }
    await this.sendBuilderMessage(command.message)
  }

  private async performRefresh(): Promise<void> {
    try {
      const result = asBootstrap(await this.input.client.request("bootstrap", {}))
      const entriesBefore = this.entries
      const selectedKey = entriesBefore[this.current.selectedIndex]?.key
      this.current = { ...this.current, snapshot: result }
      const entriesAfter = this.entries
      const selectedIndex = selectedKey
        ? entriesAfter.findIndex((entry) => entry.key === selectedKey)
        : this.current.selectedIndex
      this.patch({
        selectedIndex: selectedIndex >= 0
          ? selectedIndex
          : Math.min(this.current.selectedIndex, Math.max(0, entriesAfter.length - 1)),
      })
      await this.loadCurrentRoute()
    } catch (error) {
      this.patch({ notice: errorMessage(error) })
    }
  }

  private async loadCurrentRoute(): Promise<void> {
    const route = this.current.route
    if (route.type === "builder") return

    const generation = ++this.routeLoadGeneration
    const managesRouteBusy = this.routeBusy
    try {
      if (route.type === "system") {
        const detail = asSystemDetail(
          await this.input.client.request("system.detail", { system_id: route.systemID }),
        )
        if (!this.isCurrentRouteLoad(generation, route)) return
        this.patch({ systemDetail: detail, runDetail: null })
        return
      }

      const detail = asRunDetail(
        await this.input.client.request("run.detail", {
          run_id: route.runID,
          application_id: route.applicationID,
          ...(route.systemID ? { system_id: route.systemID } : {}),
        }),
      )
      if (!this.isCurrentRouteLoad(generation, route)) return
      const selectedKey = this.entries[this.current.selectedIndex]?.key
      const snapshot = mergeRunSummary(this.current.snapshot, detail.summary)
      const groups = buildSidebarGroups(snapshot)
      const entries = [...groups.systems, ...groups.runs]
      const selectedIndex = selectedKey
        ? entries.findIndex((entry) => entry.key === selectedKey)
        : this.current.selectedIndex
      this.patch({
        snapshot,
        selectedIndex: selectedIndex >= 0
          ? selectedIndex
          : Math.min(this.current.selectedIndex, Math.max(0, entries.length - 1)),
        runDetail: detail,
        systemDetail: null,
      })
    } catch (error) {
      if (!this.isCurrentRouteLoad(generation, route)) return
      this.patch({ notice: errorMessage(error) })
    } finally {
      if (managesRouteBusy && generation === this.routeLoadGeneration) {
        this.routeBusy = false
        this.patch({})
      }
    }
  }

  private isCurrentRouteLoad(generation: number, route: AppRoute): boolean {
    if (generation !== this.routeLoadGeneration) return false
    if (route.type === "system") {
      return this.current.route.type === "system"
        && this.current.route.systemID === route.systemID
    }
    if (route.type === "run") {
      return this.current.route.type === "run"
        && this.current.route.runID === route.runID
        && this.current.route.applicationID === route.applicationID
        && this.current.route.systemID === route.systemID
    }
    return this.current.route.type === "builder"
  }

  private async sendBuilderMessage(message: string): Promise<void> {
    this.builderBusy = true
    this.patch({
      route: { type: "builder" },
      notice: null,
      messages: [
        ...this.current.messages,
        { id: this.nextMessageID(), role: "user", content: message },
      ],
    })
    try {
      const result = asBuilderSend(
        await this.input.client.request("builder.send", {
          session_id: this.sessionID,
          message,
          ...(this.current.modelType ? { model_type: this.current.modelType } : {}),
        }),
      )
      this.patch({
        modelType: result.model_type ?? this.current.modelType,
        draft: result.draft,
        messages: [
          ...this.current.messages,
          { id: this.nextMessageID(), role: "assistant", content: result.assistant },
        ],
      })
    } catch (error) {
      await this.syncBuilderDraft(errorMessage(error))
    } finally {
      this.builderBusy = false
      this.patch({})
    }
  }

  private async applyDraft(): Promise<void> {
    const draft = this.current.draft
    if (!draft?.valid) {
      this.patch({ notice: "当前没有可应用的有效草稿" })
      return
    }
    this.builderBusy = true
    this.patch({ notice: null })
    try {
      const result = asApplyResult(
        await this.input.client.request("draft.apply", {
          session_id: this.sessionID,
          expected_revision: draft.revision,
        }),
      )
      if (!result.applied) {
        await this.syncBuilderDraft("草稿未应用，请刷新后重试")
        return
      }
      this.patch({
        messages: [
          ...this.current.messages,
          {
            id: this.nextMessageID(),
            role: "assistant",
            content: `已应用 draft revision ${result.revision}，写入 ${result.files.length} 个文件。`,
          },
        ],
        draft: null,
      })
      await this.refresh()
    } catch (error) {
      await this.syncBuilderDraft(errorMessage(error))
    } finally {
      this.builderBusy = false
      this.patch({})
    }
  }

  private async syncBuilderDraft(notice: string): Promise<void> {
    try {
      const draft = asBuilderDraft(
        await this.input.client.request("builder.draft", { session_id: this.sessionID }),
      )
      this.patch({ draft, notice })
    } catch {
      this.patch({ notice })
    }
  }

  private patch(patch: Partial<AgentLoomSessionState>): void {
    this.current = {
      ...this.current,
      ...patch,
      busy: this.builderBusy || this.routeBusy,
    }
    for (const listener of this.listeners) listener(this.current)
  }

  private nextMessageID(): number {
    this.messageID += 1
    return this.messageID
  }
}

function mergeRunSummary(
  snapshot: BootstrapResultDto,
  summary: RunSummaryDto,
): BootstrapResultDto {
  const runs = snapshot.runs.map((run) =>
    run.run_id === summary.run_id && run.application_id === summary.application_id
      ? summary
      : run,
  )
  const linkedRuns = summary.system_id === null
    ? []
    : runs.filter((run) => run.system_id === summary.system_id)
  const latestRun = linkedRuns[0] ?? null
  const linkedState: SystemSummaryDto["state"] = linkedRuns.some((run) => run.status === "running")
    ? "running"
    : latestRun?.status ?? "never_run"
  const systems = snapshot.systems.map((system) => {
    if (system.id !== summary.system_id) return system
    return { ...system, state: linkedState, latest_run: latestRun }
  })
  return { ...snapshot, systems, runs }
}

function modelCatalogMessage(snapshot: BootstrapResultDto): string {
  const configured = snapshot.models.items.filter((item) => item.configured)
  if (configured.length === 0) return "当前没有已配置的模型。"
  return [
    "可选模型：",
    ...configured.map((item) => {
      const marker = item.default ? " (默认)" : ""
      const description = item.description ? ` — ${item.description}` : ""
      return `- ${item.type}${marker}${description}`
    }),
  ].join("\n")
}

function asBootstrap(value: unknown): BootstrapResultDto {
  if (!isRecord(value) || !Array.isArray(value.systems) || !Array.isArray(value.runs)) {
    throw new Error("bootstrap 返回格式无效")
  }
  return value as unknown as BootstrapResultDto
}

function asSystemDetail(value: unknown): SystemDetailResultDto {
  if (!isRecord(value) || !isRecord(value.summary) || !isRecord(value.definition)) {
    throw new Error("system.detail 返回格式无效")
  }
  return value as unknown as SystemDetailResultDto
}

function asRunDetail(value: unknown): RunDetailResultDto {
  if (!isRecord(value) || !isRecord(value.summary) || !Array.isArray(value.workers)) {
    throw new Error("run.detail 返回格式无效")
  }
  return value as unknown as RunDetailResultDto
}

function asBuilderSend(value: unknown): {
  session_id: string
  assistant: string
  model_type: string | null
  draft: BuilderDraft
} {
  if (
    !isRecord(value)
    || typeof value.session_id !== "string"
    || typeof value.assistant !== "string"
    || (value.model_type !== null && typeof value.model_type !== "string")
    || !isDraft(value.draft)
  ) {
    throw new Error("builder.send 返回格式无效")
  }
  return value as {
    session_id: string
    assistant: string
    model_type: string | null
    draft: BuilderDraft
  }
}

function asBuilderDraft(value: unknown): BuilderDraft {
  if (!isDraft(value)) throw new Error("builder.draft 返回格式无效")
  return value
}

function asApplyResult(value: unknown): { applied: boolean; revision: number; files: string[] } {
  if (
    !isRecord(value)
    || typeof value.applied !== "boolean"
    || typeof value.revision !== "number"
    || !Array.isArray(value.files)
  ) {
    throw new Error("draft.apply 返回格式无效")
  }
  return {
    applied: value.applied,
    revision: value.revision,
    files: value.files.map(String),
  }
}

function isDraft(value: unknown): value is BuilderDraft {
  return isRecord(value)
    && typeof value.revision === "number"
    && typeof value.valid === "boolean"
    && Array.isArray(value.errors)
    && value.errors.every((item) => typeof item === "string")
    && Array.isArray(value.files)
    && value.files.every((item) => (
      isRecord(item)
      && typeof item.path === "string"
      && typeof item.change === "string"
      && typeof item.content === "string"
    ))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
