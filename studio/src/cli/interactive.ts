import { join } from "node:path"
import { runStudio, type RunStudioInput, type StudioExitReason } from "../app"
import type { StudioClient, StudioBackend } from "../app/session"
import { startOpenCodeStudio } from "../studio"

type StudioHandle = {
  client: StudioClient
  close(): Promise<void>
}

type InteractiveDependencies = {
  startStudio(input: { command: string; projectRoot: string }): Promise<StudioHandle>
  runStudio(input: RunStudioInput): Promise<StudioExitReason | void>
  restart?(projectRoot: string): void
}

const productionDependencies: InteractiveDependencies = {
  startStudio: startOpenCodeStudio,
  runStudio,
  restart: restartAgentLoom,
}

export async function runInteractiveStudio(
  input: {
    backend: StudioBackend
    projectRoot: string
    openCodeCommand?: string
  },
  dependencies: InteractiveDependencies = productionDependencies,
): Promise<void> {
  const studio = await dependencies.startStudio({
    command: input.openCodeCommand ?? resolveOpenCodeCommand(),
    projectRoot: input.projectRoot,
  })
  let exitReason: StudioExitReason | void
  try {
    exitReason = await dependencies.runStudio({
      client: input.backend,
      studio: studio.client,
      projectRoot: input.projectRoot,
    })
  } finally {
    await studio.close()
  }
  if (exitReason === "restart") (dependencies.restart ?? restartAgentLoom)(input.projectRoot)
}

export function resolveOpenCodeCommand(env = process.env): string {
  return env.AGENTLOOM_OPENCODE_BIN || join(import.meta.dir, "../../node_modules/.bin/opencode")
}

export function restartAgentLoom(
  projectRoot: string,
  env: Record<string, string | undefined> = process.env,
): void {
  const command = env.AGENTLOOM_COMMAND?.trim()
  if (!command) throw new Error("The installed AgentLoom restart command is unavailable")
  const child = Bun.spawn({
    cmd: [command, "--project", projectRoot],
    env: process.env,
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  })
  child.unref()
}
