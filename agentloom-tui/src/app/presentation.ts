import type { RunDetailResultDto, SystemDetailResultDto } from "../domain"

export type DetailSection = {
  title: string
  lines: string[]
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
