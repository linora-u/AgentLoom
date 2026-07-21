/**
 * Terminal lifecycle adapted from OpenCode packages/tui/src/app.tsx and
 * packages/tui/src/util/renderer.ts at
 * efb6cc2d4bf6332eb156709795d2b3a649198b65 (MIT).
 * See ../../upstream/LICENSE.opencode.
 */

import { createCliRenderer, type CliRenderer } from "@opentui/core"
import { render } from "@opentui/solid"
import { basename } from "node:path"
import type { BootstrapResultDto } from "../domain"
import { formatTerminalTitle } from "../ui"
import { AgentLoomSession, type StudioClient, type TuiClient } from "./session"
import { AgentLoomApp } from "./view"
import { createSourceUpdateClient, type UpdateClient } from "../update/source-updater"

export type RunTuiInput = {
  client: TuiClient
  studio: StudioClient
  snapshot?: BootstrapResultDto
  projectRoot: string
  refreshIntervalMs?: number
  updater?: UpdateClient
}

export type TuiExitReason = "exit" | "restart"

export async function runTui(input: RunTuiInput): Promise<TuiExitReason> {
  const renderer = await createCliRenderer({
    externalOutputMode: "passthrough",
    targetFps: 60,
    gatherStats: false,
    exitOnCtrlC: false,
    useKittyKeyboard: {},
    autoFocus: false,
    openConsoleOnError: false,
    useMouse: true,
  })
  const session = new AgentLoomSession({
    client: input.client,
    studio: input.studio,
    snapshot: input.snapshot,
    projectRoot: input.projectRoot,
    updater: input.updater ?? createSourceUpdateClient(),
  })
  let exitReason: TuiExitReason = "exit"
  const destroyed = waitForDestroy(renderer)
  const onSighup = () => destroyRenderer(renderer)
  process.on("SIGHUP", onSighup)
  renderer.setTerminalTitle(formatTerminalTitle(input.snapshot?.project.name ?? basename(input.projectRoot)))

  try {
    await render(
      () => (
        <AgentLoomApp
          session={session}
          projectRoot={input.projectRoot}
          refreshIntervalMs={input.refreshIntervalMs}
          onExit={() => {
            exitReason = "exit"
            destroyRenderer(renderer)
          }}
          onRestart={() => {
            exitReason = "restart"
            destroyRenderer(renderer)
          }}
        />
      ),
      renderer,
    )
    // The renderer is live before any Python import or workspace scan. Keep
    // the shell responsive and let the session publish loading/ready/error.
    void session.start()
    await destroyed
    return exitReason
  } finally {
    process.off("SIGHUP", onSighup)
    session.dispose()
    destroyRenderer(renderer)
    await input.client.close()
  }
}

function waitForDestroy(renderer: CliRenderer): Promise<void> {
  if (renderer.isDestroyed) return Promise.resolve()
  return new Promise((resolve) => renderer.once("destroy", resolve))
}

export function destroyRenderer(
  renderer: Pick<CliRenderer, "isDestroyed" | "setTerminalTitle" | "destroy">,
): void {
  renderer.setTerminalTitle("")
  if (renderer.isDestroyed) return
  renderer.destroy()
}
