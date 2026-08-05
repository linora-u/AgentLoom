import { toneColor, type ThemeMode, type ThemeTone } from "./theme"

export type ExecutionStatus =
  | "never_run"
  | "running"
  | "completed"
  | "budget_limited"
  | "interrupted"
  | "failed"
  | "crashed"
  | "unknown"
  | "incomplete"
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
  interrupted: { label: "Interrupted", symbol: "■", tone: "warning", priority: 2 },
  budget_limited: { label: "Budget limited", symbol: "!", tone: "warning", priority: 2 },
  unknown: { label: "Unknown", symbol: "?", tone: "warning", priority: 3 },
  running: { label: "Running", symbol: "●", tone: "info", priority: 4 },
  unavailable: { label: "Unavailable", symbol: "–", tone: "warning", priority: 4 },
  incomplete: { label: "Incomplete", symbol: "?", tone: "warning", priority: 4 },
  completed: { label: "Completed", symbol: "✓", tone: "success", priority: 5 },
  available: { label: "Available", symbol: "✓", tone: "success", priority: 5 },
  never_run: { label: "Never run", symbol: "○", tone: "muted", priority: 6 },
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
