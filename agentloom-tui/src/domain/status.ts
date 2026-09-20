export const builderSessionStatuses = ["idle", "busy", "retry"] as const
export type BuilderSessionStatus = (typeof builderSessionStatuses)[number]

export const runtimeStatuses = [
  "never_run",
  "running",
  "completed",
  "interrupted",
  "failed",
  "crashed",
  "unknown",
] as const
export type RuntimeStatus = (typeof runtimeStatuses)[number]
export type ObservedRuntimeStatus = RuntimeStatus

const runtimeStatusAliases: Readonly<Record<string, RuntimeStatus>> = {
  never_run: "never_run",
  running: "running",
  claimed: "running",
  in_progress: "running",
  completed: "completed",
  succeeded: "completed",
  success: "completed",
  cached: "completed",
  interrupted: "interrupted",
  cancelled: "interrupted",
  canceled: "interrupted",
  failed: "failed",
  error: "failed",
  crashed: "crashed",
  crash: "crashed",
  unknown: "unknown",
}

export function builderSessionStatus(value: string): BuilderSessionStatus {
  if (builderSessionStatuses.some((status) => status === value)) return value as BuilderSessionStatus
  return "idle"
}

export function runtimeStatus(value: string): ObservedRuntimeStatus {
  return runtimeStatusAliases[value.trim().toLowerCase()] ?? "unknown"
}

export function isProblemRuntimeStatus(value: string): boolean {
  return ["interrupted", "failed", "crashed", "unknown"].includes(runtimeStatus(value))
}
