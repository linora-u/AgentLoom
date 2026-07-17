import type { BootstrapResultDto, RuntimeStatus } from "../domain"
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
}

export type SidebarEntry = SidebarSystemEntry | SidebarRunEntry

export type AppRoute =
  | { type: "builder" }
  | { type: "system"; systemID: string }
  | { type: "run"; runID: string; applicationID: string; systemID: string | null }

export type BuilderCommand =
  | { type: "apply" }
  | { type: "refresh" }
  | { type: "models" }
  | { type: "model"; modelType: string }
  | { type: "send"; message: string }
  | { type: "empty" }

export function buildSidebarGroups(snapshot: BootstrapResultDto): {
  systems: SidebarSystemEntry[]
  runs: SidebarRunEntry[]
} {
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
    })),
    (entry) => entry.status,
  )
  return { systems, runs }
}

export function routeForEntry(entry: SidebarEntry): AppRoute {
  if (entry.kind === "system") return { type: "system", systemID: entry.systemID }
  return {
    type: "run",
    runID: entry.runID,
    applicationID: entry.applicationID,
    systemID: entry.systemID,
  }
}

export function nextSelection(current: number, delta: number, count: number): number {
  if (count <= 0) return 0
  return (current + delta + count) % count
}

export function parseBuilderInput(raw: string): BuilderCommand {
  const input = raw.trim()
  if (!input) return { type: "empty" }
  if (input === "/apply") return { type: "apply" }
  if (input === "/refresh") return { type: "refresh" }
  if (input === "/models") return { type: "models" }
  if (input.startsWith("/model ")) {
    const modelType = input.slice("/model ".length).trim()
    return modelType ? { type: "model", modelType } : { type: "empty" }
  }
  return { type: "send", message: input }
}
