import { resolve } from "node:path"
import type { BootstrapResultDto, RuntimeStatus } from "../domain"

export type CliArgs = {
  projectRoot: string
  help: boolean
  snapshot: boolean
  version: boolean
}

export function parseCliArgs(argv: readonly string[], cwd = process.cwd()): CliArgs {
  let projectRoot = resolve(cwd)
  let help = false
  let snapshot = false
  let version = false

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]!
    if (argument === "-h" || argument === "--help") {
      help = true
      continue
    }
    if (argument === "-v" || argument === "--version") {
      version = true
      continue
    }
    if (argument === "--snapshot") {
      snapshot = true
      continue
    }
    if (argument === "--project") {
      const value = argv[index + 1]
      if (!value || value.startsWith("-")) throw new Error("--project requires a path")
      projectRoot = resolve(cwd, value)
      index += 1
      continue
    }
    if (argument.startsWith("--project=")) {
      const value = argument.slice("--project=".length)
      if (!value) throw new Error("--project requires a path")
      projectRoot = resolve(cwd, value)
      continue
    }
    throw new Error(`Unknown option: ${argument}`)
  }

  return { projectRoot, help, snapshot, version }
}

export function formatSnapshot(snapshot: BootstrapResultDto): string {
  const systemCounts = statusCounts(snapshot.systems.map((system) => system.state), true)
  const runCounts = statusCounts(snapshot.runs.map((run) => run.status), false)
  return JSON.stringify(
    {
      project: snapshot.project,
      default_model: snapshot.models.default,
      systems: systemCounts,
      runs: runCounts,
    },
    null,
    2,
  )
}

function statusCounts(statuses: readonly RuntimeStatus[], includeNeverRun: boolean) {
  const counts: Record<string, number> = { total: statuses.length }
  if (includeNeverRun) counts.never_run = 0
  for (const status of ["running", "completed", "failed", "crashed"] as const) counts[status] = 0
  for (const status of statuses) {
    if (status in counts) counts[status] = (counts[status] ?? 0) + 1
  }
  return counts
}
