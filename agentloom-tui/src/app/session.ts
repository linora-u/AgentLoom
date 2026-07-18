import { basename, resolve } from "node:path"
import type {
  BootstrapResultDto,
  AssistantTurnEventDto,
  RpcMethod,
  RpcParams,
  RpcResult,
  RunDetailResultDto,
  RunSummaryDto,
  RuntimeSummaryDto,
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
  subscribeEvents?(listener: (event: AssistantTurnEventDto) => void): () => void
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
  runsIncomplete: boolean
  workspacePhase: "idle" | "loading" | "ready" | "error"
  route: AppRoute
  selectedIndex: number
  sidebarOpen: boolean
  modelType: string | null
  messages: BuilderMessage[]
  streamingText: string
  activities: Array<{ name: string; state: "started" | "completed" }>
  draft: BuilderDraft | null
  systemDetail: SystemDetailResultDto | null
  runDetail: RunDetailResultDto | null
  assistantBusy: boolean
  detailBusy: boolean
  busy: boolean
  notice: string | null
}

type Listener = (state: AgentLoomSessionState) => void

export class AgentLoomSession {
  private current: AgentLoomSessionState
  private readonly listeners = new Set<Listener>()
  private refreshPromise: Promise<void> | null = null
  private startPromise: Promise<void> | null = null
  private liveRefreshPromise: Promise<void> | null = null
  private routeLoadGeneration = 0
  private builderBusy = false
  private routeBusy = false
  private messageID = 0
  private readonly unsubscribeEvents: () => void

  constructor(
    private readonly input: {
      client: TuiClient
      snapshot?: BootstrapResultDto
      projectRoot?: string
      sessionID?: string
    },
  ) {
    if (!input.snapshot && !input.projectRoot) {
      throw new Error("AgentLoomSession requires a snapshot or projectRoot")
    }
    this.sessionID = input.sessionID ?? crypto.randomUUID()
    const snapshot = input.snapshot ?? createWorkspaceShell(input.projectRoot!)
    this.current = {
      snapshot,
      runsIncomplete: false,
      workspacePhase: input.snapshot ? "ready" : "idle",
      route: { type: "builder" },
      selectedIndex: 0,
      sidebarOpen: false,
      modelType: snapshot.models.default,
      messages: [
        {
          id: this.nextMessageID(),
          role: "assistant",
          content: "我是 AgentLoom 助手。你可以问普通问题，也可以让我查看项目、分析 Agent 状态或创建 Agent YAML。任何写入都会先给出草稿，由你显式 /apply。",
        },
      ],
      streamingText: "",
      activities: [],
      draft: null,
      systemDetail: null,
      runDetail: null,
      assistantBusy: false,
      detailBusy: false,
      busy: false,
      notice: null,
    }
    this.unsubscribeEvents = input.client.subscribeEvents?.((event) => this.handleEvent(event)) ?? (() => {})
  }

  readonly sessionID: string

  dispose(): void {
    this.unsubscribeEvents()
  }

  get state(): AgentLoomSessionState {
    return this.current
  }

  get entries(): SidebarEntry[] {
    const groups = buildSidebarGroups(this.current.snapshot)
    return [...groups.systems, ...groups.runs]
  }

  start(): Promise<void> {
    if (this.current.workspacePhase === "ready") return Promise.resolve()
    if (this.startPromise) return this.startPromise
    this.patch({ workspacePhase: "loading", notice: null })
    this.startPromise = this.performRefresh(true).finally(() => {
      this.startPromise = null
    })
    return this.startPromise
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
    this.routeBusy = entry.kind === "system" || entry.kind === "run"
    this.patch({
      route: routeForEntry(entry),
      selectedIndex: index < 0 ? this.current.selectedIndex : index,
      sidebarOpen: true,
      systemDetail: null,
      runDetail: null,
      notice: null,
    })
    await this.loadCurrentRoute()
  }

  refresh(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise
    this.refreshPromise = this.performRefresh(false).finally(() => {
      this.refreshPromise = null
    })
    return this.refreshPromise
  }

  refreshLive(): Promise<void> {
    if (this.current.workspacePhase !== "ready" || this.startPromise || this.refreshPromise) {
      return Promise.resolve()
    }
    if (this.liveRefreshPromise) return this.liveRefreshPromise
    this.liveRefreshPromise = this.performLiveRefresh().finally(() => {
      this.liveRefreshPromise = null
    })
    return this.liveRefreshPromise
  }

  private async performLiveRefresh(): Promise<void> {
    try {
      const routeWasRunning = this.current.route.type === "run"
        && this.current.runDetail?.summary.status === "running"
      const result = asRuntimeSummary(
        await this.input.client.request("runtime.summary", {}),
      )

      // Status changes reorder the context entries. Preserve the user's logical
      // selection by identity instead of keeping a now-stale numeric index.
      const selectedKey = this.entries[this.current.selectedIndex]?.key
      const snapshot = mergeRuntimeSummary(this.current.snapshot, result)
      const groups = buildSidebarGroups(snapshot)
      const entries = [...groups.systems, ...groups.runs]
      const selectedIndex = selectedKey
        ? entries.findIndex((entry) => entry.key === selectedKey)
        : this.current.selectedIndex
      const currentRoute = this.current.route
      const removedCurrentRun = currentRoute.type === "run"
        && !snapshot.runs.some((run) =>
          run.run_id === currentRoute.runID
          && run.application_id === currentRoute.applicationID)
      this.patch({
        snapshot,
        runsIncomplete: result.runs_incomplete,
        ...(removedCurrentRun
          ? { route: { type: "builder" as const }, runDetail: null }
          : {}),
        selectedIndex: selectedIndex >= 0
          ? selectedIndex
          : Math.min(this.current.selectedIndex, Math.max(0, entries.length - 1)),
      })

      // The lightweight summary updates the whole project. Only fetch the
      // heavier event/log payload when the currently open run was still live.
      if (routeWasRunning && !removedCurrentRun && this.current.route.type === "run") {
        await this.loadCurrentRoute()
      }
    } catch (error) {
      this.patch({ notice: errorMessage(error) })
    }
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
    if (command.type === "invalid") {
      this.patch({ notice: command.message })
      return
    }
    if (command.type === "schedule.help") {
      this.patch({
        messages: [
          ...this.current.messages,
          {
            id: this.nextMessageID(),
            role: "assistant",
            content: scheduleHelpMessage(this.current.snapshot),
          },
        ],
        notice: null,
      })
      return
    }
    if (command.type === "schedule.add" || command.type === "schedule.mutate") {
      await this.mutateSchedule(command)
      return
    }
    await this.sendBuilderMessage(command.message)
  }

  private async performRefresh(initial: boolean): Promise<void> {
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
        workspacePhase: "ready",
        runsIncomplete: false,
        modelType: initial ? result.models.default : this.current.modelType,
        selectedIndex: selectedIndex >= 0
          ? selectedIndex
          : Math.min(this.current.selectedIndex, Math.max(0, entriesAfter.length - 1)),
      })
      await this.loadCurrentRoute()
    } catch (error) {
      this.patch({
        workspacePhase: initial ? "error" : this.current.workspacePhase,
        notice: errorMessage(error),
      })
    }
  }

  private async loadCurrentRoute(): Promise<void> {
    const route = this.current.route
    if (route.type !== "system" && route.type !== "run") return

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
    return false
  }

  private async sendBuilderMessage(message: string): Promise<void> {
    this.builderBusy = true
    this.patch({
      route: { type: "builder" },
      notice: null,
      streamingText: "",
      activities: [],
      messages: [
        ...this.current.messages,
        { id: this.nextMessageID(), role: "user", content: message },
      ],
    })
    try {
      const result = asBuilderSend(
        await this.input.client.request("assistant.send", {
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
        streamingText: "",
        activities: [],
      })
    } catch (error) {
      await this.syncBuilderDraft(errorMessage(error))
    } finally {
      this.builderBusy = false
      this.patch({ streamingText: "", activities: [] })
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

  private async mutateSchedule(
    command: Extract<ReturnType<typeof parseBuilderInput>, { type: "schedule.add" | "schedule.mutate" }>,
  ): Promise<void> {
    if (
      command.type === "schedule.add"
      && !this.current.snapshot.systems.some((system) => system.path === command.yamlPath)
    ) {
      this.patch({ notice: `Agent YAML 不在当前项目目录中: ${command.yamlPath}` })
      return
    }
    this.builderBusy = true
    this.patch({ route: { type: "builder" }, notice: null })
    try {
      const result = command.type === "schedule.add"
        ? asScheduleMutation(await this.input.client.request("schedule.add", {
            yaml_path: command.yamlPath,
            name: command.name,
            schedule: command.schedule,
          }))
        : asScheduleMutation(await this.input.client.request(
            command.action === "pause"
              ? "schedule.pause"
              : command.action === "resume"
                ? "schedule.resume"
                : "schedule.remove",
            { job_id: command.jobID },
          ))
      this.patch({
        messages: [
          ...this.current.messages,
          {
            id: this.nextMessageID(),
            role: "assistant",
            content: [
              `Schedule ${result.action}: ${result.name} (${result.job_id}) · ${result.state}`,
              command.type === "schedule.add"
                && this.current.snapshot.schedules.service.state !== "running"
                ? `调度服务当前未运行；任务不会自动触发。请另开终端运行：${schedulerServeCommand(this.current.snapshot)}`
                : "",
            ].filter(Boolean).join("\n"),
          },
        ],
      })
      await this.refresh()
    } catch (error) {
      this.patch({ notice: errorMessage(error) })
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
      assistantBusy: this.builderBusy,
      detailBusy: this.routeBusy,
      busy: this.builderBusy || this.routeBusy,
    }
    for (const listener of this.listeners) listener(this.current)
  }

  private handleEvent(event: AssistantTurnEventDto): void {
    if (event.session_id !== this.sessionID) return
    if (event.type === "turn.started") {
      this.patch({ streamingText: "", activities: [] })
      return
    }
    if (event.type === "turn.delta") {
      this.patch({ streamingText: (this.current.streamingText + event.text).slice(-32_000) })
      return
    }
    if (event.type === "turn.activity") {
      const activities = [...this.current.activities]
      if (event.state === "started") {
        activities.push({ name: event.name, state: "started" })
      } else {
        const index = activities.findLastIndex(
          (activity) => activity.name === event.name && activity.state === "started",
        )
        if (index >= 0) activities[index] = { name: event.name, state: "completed" }
        else activities.push({ name: event.name, state: "completed" })
      }
      this.patch({ activities: activities.slice(-8) })
    }
  }

  private nextMessageID(): number {
    this.messageID += 1
    return this.messageID
  }
}

export function createWorkspaceShell(projectRoot: string): BootstrapResultDto {
  const root = resolve(projectRoot)
  return {
    project: { root, name: basename(root) },
    models: { default: null, configured: false, items: [] },
    systems: [],
    runs: [],
    worker_invocations: [],
    worker_invocations_incomplete: false,
    applications: [],
    agents: [],
    skills: [],
    schedules: {
      items: [],
      service: {
        state: "stopped",
        pid: null,
        started_at: null,
        last_tick_at: null,
        last_success_at: null,
        last_error: null,
        job_count: 0,
        due_count: 0,
        claimed_count: 0,
        execution_count: 0,
      },
    },
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

function mergeRuntimeSummary(
  snapshot: BootstrapResultDto,
  summary: RuntimeSummaryDto,
): BootstrapResultDto {
  const removed = new Set(
    summary.removed_runs.map(({ application_id, run_id }) => `${application_id}\0${run_id}`),
  )
  const retained = snapshot.runs.filter(
    (run) => !removed.has(`${run.application_id}\0${run.run_id}`),
  )
  const live = summary.runs.filter(
    (run) => !removed.has(`${run.application_id}\0${run.run_id}`),
  )
  const runs = summary.runs_incomplete
    ? mergeRuntimeRunWindow(retained, live)
    : live
  const runCounts = new Map<string, { total: number; active: number }>()
  for (const run of runs) {
    const counts = runCounts.get(run.application_id) ?? { total: 0, active: 0 }
    counts.total += 1
    if (run.status === "running") counts.active += 1
    runCounts.set(run.application_id, counts)
  }
  return {
    ...snapshot,
    systems: summary.systems,
    runs,
    worker_invocations: summary.worker_invocations,
    worker_invocations_incomplete: summary.worker_invocations_incomplete,
    applications: snapshot.applications.map((application) => {
      const counts = runCounts.get(application.id) ?? { total: 0, active: 0 }
      return {
        ...application,
        run_count: counts.total,
        active_run_count: counts.active,
      }
    }),
    schedules: summary.schedules,
  }
}

function mergeRuntimeRunWindow(
  cached: RunSummaryDto[],
  liveWindow: RunSummaryDto[],
): RunSummaryDto[] {
  const byIdentity = new Map(
    cached.map((run) => [`${run.application_id}\0${run.run_id}`, run]),
  )
  for (const run of liveWindow) {
    byIdentity.set(`${run.application_id}\0${run.run_id}`, run)
  }
  return [...byIdentity.values()].sort((left, right) => {
    const byStartedAt = (right.started_at ?? "").localeCompare(left.started_at ?? "")
    return byStartedAt || right.run_id.localeCompare(left.run_id)
  })
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

function scheduleHelpMessage(snapshot: BootstrapResultDto): string {
  return [
    "定时任务命令：",
    "- /schedule add <agent.yaml> --every 2h [--timezone Asia/Shanghai] [--name \"名称\"]",
    "- /schedule add <agent.yaml> --cron \"0 9 * * *\" [--timezone Asia/Shanghai]",
    "- /schedule add <agent.yaml> --at \"2026-07-19T09:00:00+08:00\"",
    "- /schedule pause <job-id>",
    "- /schedule resume <job-id>",
    "- /schedule remove <job-id>",
    `自动触发需要保持调度服务运行：${schedulerServeCommand(snapshot)}`,
    "输入 Ctrl+P 搜索 Schedules，可查看下一次/上一次运行与服务状态。",
  ].join("\n")
}

function schedulerServeCommand(snapshot: BootstrapResultDto): string {
  return `agentloom schedules --project ${shellQuote(snapshot.project.root)} serve`
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

function asBootstrap(value: unknown): BootstrapResultDto {
  if (
    !isRecord(value)
    || !Array.isArray(value.systems)
    || !Array.isArray(value.runs)
    || !Array.isArray(value.worker_invocations)
    || typeof value.worker_invocations_incomplete !== "boolean"
  ) {
    throw new Error("bootstrap 返回格式无效")
  }
  return value as unknown as BootstrapResultDto
}

function asRuntimeSummary(value: unknown): RuntimeSummaryDto {
  if (
    !isRecord(value)
    || !Array.isArray(value.systems)
    || !Array.isArray(value.runs)
    || typeof value.runs_incomplete !== "boolean"
    || !Array.isArray(value.removed_runs)
    || !value.removed_runs.every((removed) =>
      isRecord(removed)
      && typeof removed.application_id === "string"
      && typeof removed.run_id === "string")
    || !Array.isArray(value.worker_invocations)
    || typeof value.worker_invocations_incomplete !== "boolean"
    || !isRecord(value.schedules)
    || !Array.isArray(value.schedules.items)
    || !isRecord(value.schedules.service)
  ) {
    throw new Error("runtime.summary 返回格式无效")
  }
  return value as unknown as RuntimeSummaryDto
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
    throw new Error("assistant.send 返回格式无效")
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

function asScheduleMutation(value: unknown): {
  action: "add" | "pause" | "resume" | "remove"
  job_id: string
  name: string
  state: string
} {
  if (
    !isRecord(value)
    || !["add", "pause", "resume", "remove"].includes(String(value.action))
    || typeof value.job_id !== "string"
    || typeof value.name !== "string"
    || typeof value.state !== "string"
  ) {
    throw new Error("Schedule 操作返回格式无效")
  }
  return value as {
    action: "add" | "pause" | "resume" | "remove"
    job_id: string
    name: string
    state: string
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
