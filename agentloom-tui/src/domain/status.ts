export const builderSessionStatuses = ["idle", "busy", "retry"] as const
export type BuilderSessionStatus = (typeof builderSessionStatuses)[number]

export const runtimeStatuses = ["never_run", "running", "completed", "failed", "crashed"] as const
export type RuntimeStatus = (typeof runtimeStatuses)[number]
export type ObservedRuntimeStatus = RuntimeStatus | "unknown"

export function builderSessionStatus(value: string): BuilderSessionStatus {
  if (builderSessionStatuses.some((status) => status === value)) return value as BuilderSessionStatus
  return "idle"
}

export function runtimeStatus(value: string): ObservedRuntimeStatus {
  if (runtimeStatuses.some((status) => status === value)) return value as RuntimeStatus
  return "unknown"
}
