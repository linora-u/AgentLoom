import type {
  AgentCatalogDto,
  BootstrapResultDto,
  RunDetailResultDto,
  ScheduleTriggerDto,
  SystemDetailResultDto,
} from "../domain"
import type { AppRoute } from "./controller"

export type DetailSection = {
  title: string
  lines: string[]
}

export type WorkspaceEntityDetail = {
  title: string
  subtitle: string
  sections: DetailSection[]
}

export function workspaceEntityDetail(
  snapshot: BootstrapResultDto,
  route: AppRoute,
): WorkspaceEntityDetail | null {
  if (route.type === "application") {
    const application = snapshot.applications.find((item) => item.id === route.applicationID)
    if (!application) return null
    const agents = flattenAgents(snapshot.agents).filter(
      (item) => item.agent.application_id === application.id,
    )
    const skills = snapshot.skills.filter((item) => item.application_id === application.id)
    const runs = snapshot.runs.filter((item) => item.application_id === application.id)
    return {
      title: "Application",
      subtitle: application.name,
      sections: compactSections([
        {
          title: "概览",
          lines: [
            `路径: ${application.path}`,
            `Supervisors: ${application.system_count}`,
            `Workers: ${application.worker_count}`,
            `Skills: ${application.skill_count}`,
            `Runs: ${application.run_count}`,
            `当前运行: ${application.active_run_count}`,
          ],
        },
        {
          title: "Agents",
          lines: agents.map(({ agent, depth }) => `${"  ".repeat(depth)}${agent.name} (${agent.role})`),
        },
        {
          title: "Skills",
          lines: skills.map((skill) => `${skill.name} — ${skill.description || skill.path}`),
        },
        {
          title: "最近运行",
          lines: runs.slice(0, 20).map((run) => `${run.run_id} — ${run.status}`),
        },
      ]),
    }
  }

  if (route.type === "agent") {
    const node = flattenAgents(snapshot.agents).find((item) => item.agent.id === route.agentID)
    if (!node) return null
    const agent = node.agent
    const system = snapshot.systems.find((item) => item.id === route.systemID)
    const parent = node.parentID
      ? flattenAgents(snapshot.agents).find((item) => item.agent.id === node.parentID)?.agent
      : null
    const invocation = agent.role === "worker"
      ? snapshot.worker_invocations.find(
          (item) => item.system_id === route.systemID && item.agent_name === agent.name,
        )
      : undefined
    const runtime = agent.role === "worker"
      ? invocation
        ? [
            "运行范围: 由 Supervisor Run 调用，不存在独立的项目级 Run",
            `Agent 状态: ${invocation.status}`,
            `父 Agent 状态: ${system?.state ?? "unknown"}`,
            `Run: ${invocation.run_id}`,
            `调用: #${invocation.call_index}`,
            `Step: ${invocation.step ?? "—"}`,
            `开始: ${invocation.started_at ?? "—"}`,
            `结束: ${invocation.ended_at ?? "—"}`,
            `错误: ${invocation.error ?? "—"}`,
          ]
        : [
            "运行范围: 由 Supervisor Run 调用，不存在独立的项目级 Run",
            `Agent 状态: ${snapshot.worker_invocations_incomplete ? "incomplete" : "never_run"}`,
            `父 Agent 状态: ${system?.state ?? "unknown"}`,
            snapshot.worker_invocations_incomplete
              ? "Worker 状态投影不完整，无法判断该 Agent 是否运行过"
              : "尚无 Worker 调用记录",
          ]
      : [
          `Agent 状态: ${system?.state ?? "unknown"}`,
          `最近 Run: ${system?.latest_run?.run_id ?? "—"}`,
        ]
    return {
      title: agent.role === "worker" ? "Worker Agent" : "Agent",
      subtitle: agent.name,
      sections: compactSections([
        {
          title: "定义",
          lines: [
            `角色: ${agent.role}`,
            `Application: ${agent.application_id}`,
            `说明: ${agent.description || "—"}`,
            `路径: ${agent.path}`,
            `父 Agent: ${parent?.name ?? "—"}`,
          ],
        },
        {
          title: "Skills",
          lines: [
            `加载模式: ${agent.skills.load_mode ?? "未指定"}`,
            ...agent.skills.items,
          ],
        },
        {
          title: "子 Agents",
          lines: agent.workers.map((worker) => `${worker.name} — ${worker.description || worker.path}`),
        },
        { title: "运行", lines: runtime },
      ]),
    }
  }

  if (route.type === "skill") {
    const skill = snapshot.skills.find((item) => item.id === route.skillID)
    if (!skill) return null
    const configuredBy = flattenAgents(snapshot.agents)
      .filter(({ agent }) => agent.skills.items.includes(skill.name) || agent.skills.items.includes(skill.path))
      .map(({ agent }) => agent.name)
    return {
      title: "Skill",
      subtitle: skill.name,
      sections: compactSections([
        {
          title: "定义",
          lines: [
            `Application: ${skill.application_id}`,
            `来源: ${skill.origin}`,
            `说明: ${skill.description || "—"}`,
            `路径: ${skill.path}`,
          ],
        },
        { title: "配置到 Agents", lines: configuredBy },
      ]),
    }
  }

  if (route.type === "schedule") {
    const schedule = snapshot.schedules.items.find((item) => item.id === route.scheduleID)
    if (!schedule) return null
    const execution = schedule.last_execution
    const service = snapshot.schedules.service
    return {
      title: "Schedule",
      subtitle: schedule.name,
      sections: compactSections([
        {
          title: "计划",
          lines: [
            `状态: ${schedule.state}`,
            `启用: ${schedule.enabled ? "是" : "否"}`,
            `触发: ${scheduleTriggerText(schedule.trigger)}`,
            `Agent YAML: ${schedule.yaml_path ?? "—"}`,
            `下次运行: ${schedule.next_run_at ?? "—"}`,
          ],
        },
        {
          title: "历史",
          lines: [
            `运行次数: ${schedule.run_count}`,
            `上次运行: ${schedule.last_run_at ?? "—"}`,
            `上次状态: ${schedule.last_status ?? "—"}`,
            ...(execution
              ? [
                  `Execution: ${execution.id}`,
                  `开始: ${execution.started_at ?? "—"}`,
                  `结束: ${execution.finished_at ?? "—"}`,
                  ...(execution.error ? [`错误: ${execution.error}`] : []),
                ]
              : []),
          ],
        },
        {
          title: "服务",
          lines: [
            `调度服务: ${service.state}`,
            `PID: ${service.pid ?? "—"}`,
            `上次 tick: ${service.last_tick_at ?? "—"}`,
            `执行总数: ${service.execution_count}`,
            ...(service.last_error ? [`服务错误: ${service.last_error}`] : []),
          ],
        },
        {
          title: "操作",
          lines: [
            schedule.enabled
              ? `/schedule pause ${schedule.id}`
              : `/schedule resume ${schedule.id}`,
            `/schedule remove ${schedule.id}`,
            "输入 /schedule 查看创建命令",
          ],
        },
      ]),
    }
  }

  return null
}

type FlatAgent = {
  agent: AgentCatalogDto
  parentID: string | null
  depth: number
}

function flattenAgents(roots: AgentCatalogDto[]): FlatAgent[] {
  const result: FlatAgent[] = []
  const visit = (agent: AgentCatalogDto, parentID: string | null, depth: number) => {
    result.push({ agent, parentID, depth })
    for (const worker of agent.workers) visit(worker, agent.id, depth + 1)
  }
  for (const root of roots) visit(root, null, 0)
  return result
}

function scheduleTriggerText(trigger: ScheduleTriggerDto): string {
  if (trigger.kind === "cron") return `cron ${trigger.expression} (${trigger.timezone})`
  if (trigger.kind === "interval") return `every ${trigger.seconds}s (${trigger.timezone})`
  if (trigger.kind === "once") return `once ${trigger.at ?? "—"} (${trigger.timezone})`
  return "unknown"
}

export function systemDetailSections(detail: SystemDetailResultDto): DetailSection[] {
  const workflow = Array.isArray(detail.definition.workflow)
    ? detail.definition.workflow.join(" → ")
    : detail.definition.workflow
  const validation = detail.summary.validation.valid
    ? ["校验: 通过"]
    : ["校验: 失败", ...detail.summary.validation.errors.map((error) => `  ${error}`)]
  const execution = detail.execution.state === "never_run"
    ? ["尚未运行，无执行结果"]
    : [
        `执行状态: ${detail.execution.state}`,
        detail.execution.latest_run
          ? `最近运行: ${detail.execution.latest_run.run_id}`
          : "最近运行: 无",
      ]

  return compactSections([
    {
      title: "定义",
      lines: [
        `名称: ${detail.definition.name}`,
        `说明: ${detail.definition.description || "—"}`,
        `模型: ${detail.definition.model_type ?? "未指定"}`,
        `工作流: ${workflow || "—"}`,
        `路径: ${detail.definition.path}`,
        ...validation,
      ],
    },
    {
      title: "拓扑",
      lines: [
        `Supervisor: ${detail.topology.supervisor.name}`,
        ...detail.topology.workers.map((worker) => `${worker.name} — ${worker.description || worker.path}`),
      ],
    },
    {
      title: "文件",
      lines: detail.files.map((file) => `${file.path} (${file.kind}, ${formatBytes(file.size)})`),
    },
    { title: "执行", lines: execution },
  ])
}

export function runDetailSections(detail: RunDetailResultDto): DetailSection[] {
  const summary = detail.summary
  const result = (() => {
    if (detail.result_state === "available") {
      return [
        ...(detail.limits.result.truncated
          ? [`⚠ 结果已截断（显示 ${formatBytes(detail.limits.result.returned_bytes)}）`]
          : []),
        detail.result || "（结果为空）",
      ]
    }
    if (detail.result_state === "running") return ["运行中，结果尚未生成"]
    if (detail.result_state === "unavailable") {
      return detail.limits.result.source_incomplete
        ? ["事件源已截断，无法确认该次运行是否保留结果"]
        : ["该次运行未保留可读取的结果"]
    }
    return ["尚未运行，无执行结果"]
  })()

  return compactSections([
    {
      title: "运行",
      lines: [
        `Run: ${summary.run_id}`,
        `Agent: ${summary.agent_name}`,
        `状态: ${summary.status}`,
        `Task: ${summary.task_id ?? "—"}`,
        `开始: ${summary.started_at ?? "—"}`,
        `结束: ${summary.ended_at ?? "—"}`,
        ...(detail.error ? [`错误: ${detail.error}`] : []),
      ],
    },
    {
      title: "Workers",
      lines: [
        ...(detail.limits.workers.truncated
          ? [`⚠ Worker 列表已截断（最多 ${detail.limits.workers.max_count} 项）`]
          : []),
        ...detail.workers.map((worker) => {
          const step = worker.step === null ? "" : ` — step ${worker.step}`
          const error = worker.error ? ` — ${worker.error}` : ""
          return `${worker.agent_name} #${worker.call_index} — ${worker.status}${step}${error}`
        }),
      ],
    },
    {
      title: "Events",
      lines: [
        ...(detail.limits.events.truncated
          ? [
              `⚠ 事件已截断（显示最近 ${detail.limits.events.returned_count} 条 / ${formatBytes(detail.limits.events.returned_bytes)}）`,
            ]
          : []),
        ...detail.events.map((event) => safeJson(event)),
      ],
    },
    {
      title: "Logs",
      lines: [
        ...(detail.limits.logs.truncated
          ? [
              `⚠ 日志已截断（${detail.limits.logs.returned_count} 个文件 / ${formatBytes(detail.limits.logs.returned_bytes)}）`,
            ]
          : []),
        ...detail.logs.flatMap((log) => [
          `${log.path} (${formatBytes(log.size)})${log.tail_truncated ? " — 日志尾部已截断" : ""}`,
          ...(log.tail ? log.tail.split("\n").map((line) => `  ${line}`) : []),
        ]),
      ],
    },
    {
      title: "Artifacts",
      lines: [
        ...(detail.limits.artifacts.truncated
          ? [`⚠ 文件列表已截断（最多 ${detail.limits.artifacts.max_count} 项）`]
          : []),
        ...detail.artifacts.map((artifact) => `${artifact.path} (${formatBytes(artifact.size)})`),
      ],
    },
    { title: "结果", lines: result },
  ])
}

function compactSections(sections: DetailSection[]): DetailSection[] {
  return sections.map((section) => ({
    ...section,
    lines: section.lines.length ? section.lines : ["—"],
  }))
}

function safeJson(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value)
  } catch {
    return "[无法序列化的事件]"
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
