/**
 * Terminal lifecycle adapted from OpenCode packages/tui/src/app.tsx and
 * packages/tui/src/util/renderer.ts at
 * efb6cc2d4bf6332eb156709795d2b3a649198b65 (MIT).
 * See ../../upstream/LICENSE.opencode.
 */

import { createCliRenderer, type CliRenderer } from "@opentui/core"
import { render } from "@opentui/solid"
import type { BootstrapResultDto } from "../domain"
import { formatTerminalTitle } from "../ui"
import { AgentLoomSession, type TuiClient } from "./session"
import { AgentLoomApp } from "./view"

export type RunTuiInput = {
  client: TuiClient
  snapshot: BootstrapResultDto
  projectRoot: string
  refreshIntervalMs?: number
}

export async function runTui(input: RunTuiInput): Promise<void> {
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
  const session = new AgentLoomSession({ client: input.client, snapshot: input.snapshot })
  const destroyed = waitForDestroy(renderer)
  const onSighup = () => destroyRenderer(renderer)
  process.on("SIGHUP", onSighup)
  renderer.setTerminalTitle(formatTerminalTitle(input.snapshot.project.name))

  try {
    await render(
      () => (
        <AgentLoomApp
          session={session}
          projectRoot={input.projectRoot}
          refreshIntervalMs={input.refreshIntervalMs}
          onExit={() => destroyRenderer(renderer)}
        />
      ),
      renderer,
    )
    await destroyed
  } finally {
    process.off("SIGHUP", onSighup)
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
