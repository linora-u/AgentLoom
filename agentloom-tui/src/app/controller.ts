import type { AgentCatalogDto, BootstrapResultDto, RuntimeStatus } from "../domain"
import { selectionKey } from "../domain"
import { sortByStatus } from "../ui"

export type SidebarSystemEntry = {
  kind: "system"
  key: string
  title: string
  subtitle: string
  status: RuntimeStatus
  systemID: string
}

export type SidebarRunEntry = {
  kind: "run"
  key: string
  title: string
  subtitle: string
  status: Exclude<RuntimeStatus, "never_run">
  systemID: string | null
  runID: string
  applicationID: string
  startedAt: string | null
  endedAt: string | null
}

export type SidebarApplicationEntry = {
  kind: "application"
  key: string
  title: string
  subtitle: string
  applicationID: string
  health: "healthy" | "invalid"
  runState: "never_run" | "running" | "idle"
}

export type SidebarAgentEntry = {
  kind: "agent"
  key: string
  title: string
  subtitle: string
  status: RuntimeStatus
  agentID: string
  systemID: string
  applicationID: string
  role: "supervisor" | "worker"
}

export type SidebarSkillEntry = {
  kind: "skill"
  key: string
  title: string
  subtitle: string
  skillID: string
  applicationID: string | null
}

export type SidebarScheduleEntry = {
  kind: "schedule"
  key: string
  title: string
  subtitle: string
  scheduleID: string
}

export type SidebarEntry =
  | SidebarSystemEntry
  | SidebarRunEntry
  | SidebarApplicationEntry
  | SidebarAgentEntry
  | SidebarSkillEntry
  | SidebarScheduleEntry

export type PaletteItem =
  | {
      key: string
      category: "Commands"
      title: string
      description: string
      action:
        | "new-application"
        | "chat"
        | "refresh"
        | "models"
        | "apply"
        | "schedules"
        | "permission-toggle"
        | "update"
    }
  | {
      key: string
      category: "Applications" | "Agents" | "Skills" | "Schedules" | "Runs"
      title: string
      description: string
      entry: SidebarEntry
    }
  | {
      key: string
      category: "Models"
      title: string
      description: string
      modelType: string
    }

export type AppRoute =
  | { type: "builder" }
  | { type: "system"; systemID: string }
  | { type: "run"; runID: string; applicationID: string; systemID: string | null }
  | { type: "application"; applicationID: string }
  | { type: "agent"; agentID: string; systemID: string }
  | { type: "skill"; skillID: string }
  | { type: "schedule"; scheduleID: string }

export type BuilderCommand =
  | { type: "help" }
  | { type: "new" }
  | { type: "apply" }
  | { type: "refresh" }
  | { type: "models" }
  | { type: "model"; modelType: string }
  | { type: "schedule.help" }
  | {
      type: "schedule.add"
      yamlPath: string
      name: string
      schedule:
        | { kind: "once"; at: string; timezone: string }
        | { kind: "interval"; every: string; timezone: string }
        | { kind: "cron"; expression: string; timezone: string }
    }
  | { type: "schedule.mutate"; action: "pause" | "resume" | "remove"; jobID: string }
  | { type: "invalid"; message: string }
  | { type: "send"; message: string }
  | { type: "empty" }

export function applicationHealthLabel(health: SidebarApplicationEntry["health"]): string {
  return {
    healthy: "配置有效",
    invalid: "配置有错误",
  }[health]
}

export function applicationRunStateLabel(state: SidebarApplicationEntry["runState"]): string {
  return {
    never_run: "尚未运行",
    running: "运行中",
    idle: "当前未运行",
  }[state]
}

export function buildSidebarGroups(snapshot: BootstrapResultDto): {
  applications: SidebarApplicationEntry[]
  systems: SidebarSystemEntry[]
  runs: SidebarRunEntry[]
  skills: SidebarSkillEntry[]
} {
  const applications: SidebarApplicationEntry[] = snapshot.applications.map((application) => {
    const systems = snapshot.systems.filter((system) => system.application_id === application.id)
    const health: SidebarApplicationEntry["health"] = systems.some((system) => !system.validation.valid)
      ? "invalid"
      : "healthy"
    const runState: SidebarApplicationEntry["runState"] = application.active_run_count > 0
      ? "running"
      : application.run_count === 0
        ? "never_run"
        : "idle"
    return {
      kind: "application" as const,
      key: catalogKey("application", application.id),
      title: application.id,
      subtitle: `${applicationHealthLabel(health)} · ${applicationRunStateLabel(runState)}`,
      applicationID: application.id,
      health,
      runState,
    }
  })
  const systems = sortByStatus(
    snapshot.systems.map((system) => ({
      kind: "system" as const,
      key: selectionKey({ kind: "system", systemID: system.id }),
      title: system.name,
      subtitle: system.application_id,
      status: system.state,
      systemID: system.id,
    })),
    (entry) => entry.status,
  )
  const runs = sortByStatus(
    snapshot.runs.map((run) => ({
      kind: "run" as const,
      key: selectionKey({
        kind: "run",
        applicationID: run.application_id,
        runID: run.run_id,
      }),
      title: run.agent_name || run.application_id,
      subtitle: run.run_id,
      status: run.status,
      systemID: run.system_id,
      runID: run.run_id,
      applicationID: run.application_id,
      startedAt: run.started_at,
      endedAt: run.ended_at,
    })),
    (entry) => entry.status,
  )
  const skills = snapshot.skills.map((skill) => ({
    kind: "skill" as const,
    key: catalogKey("skill", skill.id),
    title: skill.name,
    subtitle: skill.application_id ?? "Global",
    skillID: skill.id,
    applicationID: skill.application_id,
  }))
  return { applications, systems, runs, skills }
}

export function recentRunEntries(entries: readonly SidebarRunEntry[], limit = 5): SidebarRunEntry[] {
  return entries
    .toSorted((left, right) => {
      const byStartedAt = timestamp(right.startedAt) - timestamp(left.startedAt)
      return byStartedAt || right.runID.localeCompare(left.runID)
    })
    .slice(0, Math.max(0, limit))
}

export function routeForEntry(entry: SidebarEntry): AppRoute {
  if (entry.kind === "system") return { type: "system", systemID: entry.systemID }
  if (entry.kind === "run") {
    return {
      type: "run",
      runID: entry.runID,
      applicationID: entry.applicationID,
      systemID: entry.systemID,
    }
  }
  if (entry.kind === "application") {
    return { type: "application", applicationID: entry.applicationID }
  }
  if (entry.kind === "agent") {
    return { type: "agent", agentID: entry.agentID, systemID: entry.systemID }
  }
  if (entry.kind === "skill") return { type: "skill", skillID: entry.skillID }
  return { type: "schedule", scheduleID: entry.scheduleID }
}

export function buildPaletteItems(snapshot: BootstrapResultDto): PaletteItem[] {
  const groups = buildSidebarGroups(snapshot)
  const applicationItems: PaletteItem[] = groups.applications.map((application) => ({
    key: application.key,
    category: "Applications",
    title: application.title,
    description: application.subtitle,
    entry: application,
  }))
  const agentItems = buildAgentPaletteItems(snapshot, groups.systems)
  const skillByID = new Map(snapshot.skills.map((skill) => [skill.id, skill]))
  const skillItems: PaletteItem[] = groups.skills.map((entry) => {
    const skill = skillByID.get(entry.skillID)!
    return {
      key: entry.key,
      category: "Skills",
      title: entry.title,
      description: `${skill.application_id ?? "Global"} · ${skill.description || skill.path}`,
      entry,
    }
  })
  const scheduleItems: PaletteItem[] = snapshot.schedules.items.map((schedule) => ({
    key: catalogKey("schedule", schedule.id),
    category: "Schedules",
    title: schedule.name,
    description: `${schedule.state} · ${scheduleTriggerLabel(schedule.trigger)} · next ${schedule.next_run_at ?? "—"}`,
    entry: {
      kind: "schedule",
      key: catalogKey("schedule", schedule.id),
      title: schedule.name,
      subtitle: schedule.state,
      scheduleID: schedule.id,
    },
  }))
  return [
    {
      key: "command:new-application",
      category: "Commands",
      title: "新建 Application",
      description: "创建一个全新的 AgentLoom Application",
      action: "new-application",
    },
    {
      key: "command:chat",
      category: "Commands",
      title: "返回对话",
      description: "AgentLoom Chat",
      action: "chat",
    },
    {
      key: "command:refresh",
      category: "Commands",
      title: "刷新工作区",
      description: "重新索引 Agent 与 Run",
      action: "refresh",
    },
    {
      key: "command:permission-toggle",
      category: "Commands",
      title: "切换 Full Access",
      description: "开启或关闭当前 Studio 会话的全项目写权限",
      action: "permission-toggle",
    },
    {
      key: "command:models",
      category: "Commands",
      title: "从 llm.yaml 选择 Studio 模型",
      description: "模型类型、Provider 与认证统一读取项目 config/llm.yaml",
      action: "models",
    },
    {
      key: "command:schedules",
      category: "Commands",
      title: "管理定时任务",
      description: "创建、暂停、恢复或删除 Schedule",
      action: "schedules",
    },
    ...applicationItems,
    ...agentItems,
    ...skillItems,
    ...scheduleItems,
    ...groups.runs.map((entry) => ({
      key: entry.key,
      category: "Runs" as const,
      title: entry.title,
      description: `${entry.subtitle} · ${entry.status}`,
      entry,
    })),
  ]
}

export function buildModelPaletteItems(
  snapshot: BootstrapResultDto,
  currentModel = snapshot.models.default,
): PaletteItem[] {
  return snapshot.models.items
    .filter((model) => model.configured)
    .map((model) => ({
      key: `model:${model.type}`,
      category: "Models" as const,
      title: model.type,
      description: [
        model.type === currentModel ? "当前" : model.default ? "默认" : "可用",
        model.description,
      ].filter(Boolean).join(" · "),
      modelType: model.type,
    }))
}

export type FlatAgentCatalogEntry = {
  agent: AgentCatalogDto
  systemID: string
  parentAgentID: string | null
  depth: number
}

export function flattenAgentCatalog(snapshot: BootstrapResultDto): FlatAgentCatalogEntry[] {
  const result: FlatAgentCatalogEntry[] = []
  const visit = (
    agent: AgentCatalogDto,
    systemID: string,
    parentAgentID: string | null,
    depth: number,
  ) => {
    result.push({ agent, systemID, parentAgentID, depth })
    for (const worker of agent.workers) visit(worker, systemID, agent.id, depth + 1)
  }
  for (const root of snapshot.agents) visit(root, root.id, null, 0)
  return result
}

function buildAgentPaletteItems(
  snapshot: BootstrapResultDto,
  systems: SidebarSystemEntry[],
): PaletteItem[] {
  const supervisors = flattenAgentCatalog(snapshot).filter((node) => node.depth === 0)
  const supervisorBySystem = new Map(supervisors.map((node) => [node.systemID, node]))
  const systemStatus = new Map(systems.map((entry) => [entry.systemID, entry.status]))
  const result: PaletteItem[] = []
  const seen = new Set<string>()

  for (const system of systems) {
    const supervisor = supervisorBySystem.get(system.systemID)
    result.push(supervisor
      ? agentPaletteItem(supervisor, system.status)
      : {
          key: system.key,
          category: "Agents",
          title: system.title,
          description: `${system.subtitle} · 主 Agent · ${agentRuntimeStatusLabel(system.status)}`,
          entry: system,
        })
    seen.add(system.systemID)
  }

  for (const node of supervisors) {
    if (seen.has(node.systemID)) continue
    const status = systemStatus.get(node.systemID) ?? "never_run"
    result.push(agentPaletteItem(node, status))
  }
  return result
}

function agentPaletteItem(node: FlatAgentCatalogEntry, status: RuntimeStatus): PaletteItem {
  const key = catalogKey("agent", node.agent.id)
  return {
    key,
    category: "Agents",
    title: node.agent.name,
    description: `${node.agent.application_id} · 主 Agent · ${agentRuntimeStatusLabel(status)}`,
    entry: {
      kind: "agent",
      key,
      title: node.agent.name,
      subtitle: node.agent.application_id,
      status,
      agentID: node.agent.id,
      systemID: node.systemID,
      applicationID: node.agent.application_id,
      role: node.agent.role,
    },
  }
}

function agentRuntimeStatusLabel(status: RuntimeStatus): string {
  return {
    never_run: "尚未运行",
    running: "运行中",
    completed: "已完成",
    interrupted: "已中断",
    failed: "失败",
    crashed: "崩溃",
    unknown: "状态未知",
  }[status]
}

function timestamp(value: string | null): number {
  if (!value) return Number.NEGATIVE_INFINITY
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY
}

function catalogKey(kind: string, id: string): string {
  return `${kind.length}:${kind}|${id.length}:${id}`
}

function scheduleTriggerLabel(trigger: BootstrapResultDto["schedules"]["items"][number]["trigger"]): string {
  if (trigger.kind === "cron") return `cron ${trigger.expression}`
  if (trigger.kind === "interval") return `every ${trigger.seconds}s`
  if (trigger.kind === "once") return `once ${trigger.at ?? "—"}`
  return "unknown trigger"
}

export function nextSelection(current: number, delta: number, count: number): number {
  if (count <= 0) return 0
  return (current + delta + count) % count
}

export function parseBuilderInput(raw: string): BuilderCommand {
  const input = raw.trim()
  if (!input) return { type: "empty" }
  if (input === "/help") return { type: "help" }
  if (input === "/new") return { type: "new" }
  if (input === "/apply") return { type: "apply" }
  if (input === "/refresh") return { type: "refresh" }
  if (input === "/models") return { type: "models" }
  if (input.startsWith("/model ")) {
    const modelType = input.slice("/model ".length).trim()
    return modelType ? { type: "model", modelType } : { type: "empty" }
  }
  if (input === "/schedule" || input === "/schedule help") return { type: "schedule.help" }
  if (input.startsWith("/schedule ")) return parseScheduleInput(input)
  return { type: "send", message: input }
}

function parseScheduleInput(input: string): BuilderCommand {
  if (/--cron\s+[^'"\s]+\s+\S+/.test(input) && !/--cron\s+['"]/.test(input)) {
    return { type: "invalid", message: "cron 表达式包含空格时必须使用引号" }
  }
  const words = commandWords(input)
  if (!words) return { type: "invalid", message: "定时任务命令存在未闭合的引号或转义" }
  const action = words[1]
  if (action === "pause" || action === "resume" || action === "remove") {
    return words.length === 3 && words[2]
      ? { type: "schedule.mutate", action, jobID: words[2] }
      : { type: "invalid", message: `用法: /schedule ${action} <job-id>` }
  }
  if (action !== "add") return { type: "invalid", message: "未知定时任务命令；输入 /schedule 查看用法" }
  const yamlPath = words[2]
  if (!yamlPath) return { type: "invalid", message: "定时任务缺少 Agent YAML 路径" }

  let name = ""
  let timezone = "UTC"
  let trigger:
    | { kind: "once"; at: string; timezone: string }
    | { kind: "interval"; every: string; timezone: string }
    | { kind: "cron"; expression: string; timezone: string }
    | undefined
  for (let index = 3; index < words.length; index += 2) {
    const flag = words[index]
    const value = words[index + 1]
    if (!flag?.startsWith("--") || !value) {
      return { type: "invalid", message: "定时任务参数必须使用 --flag value" }
    }
    if (flag === "--name") name = value
    else if (flag === "--timezone") timezone = value
    else if (flag === "--every") {
      if (trigger) return { type: "invalid", message: "只能选择 --at、--every、--cron 之一" }
      trigger = { kind: "interval", every: value, timezone }
    } else if (flag === "--at") {
      if (trigger) return { type: "invalid", message: "只能选择 --at、--every、--cron 之一" }
      trigger = { kind: "once", at: value, timezone }
    } else if (flag === "--cron") {
      if (trigger) return { type: "invalid", message: "只能选择 --at、--every、--cron 之一" }
      trigger = { kind: "cron", expression: value, timezone }
    } else {
      return { type: "invalid", message: `未知定时任务参数: ${flag}` }
    }
  }
  if (!trigger) return { type: "invalid", message: "必须选择 --at、--every、--cron 之一" }
  trigger = { ...trigger, timezone }
  return { type: "schedule.add", yamlPath, name, schedule: trigger }
}

function commandWords(input: string): string[] | null {
  const words: string[] = []
  let current = ""
  let quote: "'" | '"' | null = null
  let escaping = false
  for (const character of input) {
    if (escaping) {
      current += character
      escaping = false
      continue
    }
    if (character === "\\") {
      escaping = true
      continue
    }
    if (quote) {
      if (character === quote) quote = null
      else current += character
      continue
    }
    if (character === "'" || character === '"') {
      quote = character
      continue
    }
    if (/\s/.test(character)) {
      if (current) {
        words.push(current)
        current = ""
      }
      continue
    }
    current += character
  }
  if (quote || escaping) return null
  if (current) words.push(current)
  return words
}
