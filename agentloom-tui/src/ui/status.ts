import { toneColor, type ThemeMode, type ThemeTone } from "./theme"

export type ExecutionStatus = "never_run" | "running" | "completed" | "failed" | "crashed" | "incomplete"
export type ResultStatus = "never_run" | "running" | "available" | "unavailable"
export type AgentLoomStatus = ExecutionStatus | ResultStatus

export type StatusPresentation = {
  label: string
  symbol: string
  tone: ThemeTone
  priority: number
}

const presentations = {
  crashed: { label: "Crashed", symbol: "×", tone: "error", priority: 0 },
  failed: { label: "Failed", symbol: "×", tone: "error", priority: 1 },
  running: { label: "Running", symbol: "●", tone: "info", priority: 2 },
  unavailable: { label: "Unavailable", symbol: "–", tone: "warning", priority: 3 },
  incomplete: { label: "Incomplete", symbol: "?", tone: "warning", priority: 3 },
  completed: { label: "Completed", symbol: "✓", tone: "success", priority: 4 },
  available: { label: "Available", symbol: "✓", tone: "success", priority: 4 },
  never_run: { label: "Never run", symbol: "○", tone: "muted", priority: 5 },
} as const satisfies Record<AgentLoomStatus, StatusPresentation>

export function statusPresentation(status: AgentLoomStatus): StatusPresentation {
  return presentations[status]
}

export function statusColor(status: AgentLoomStatus, mode: ThemeMode): string {
  return toneColor(presentations[status].tone, mode)
}

export function sortByStatus<Item>(
  items: readonly Item[],
  statusOf: (item: Item) => AgentLoomStatus,
): Item[] {
  return items
    .map((item, index) => ({ item, index, priority: presentations[statusOf(item)].priority }))
    .toSorted((left, right) => left.priority - right.priority || left.index - right.index)
    .map((entry) => entry.item)
}
