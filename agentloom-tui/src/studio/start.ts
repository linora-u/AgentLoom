import type { StudioClient } from "../app/session"
import { OpenCodeRuntime } from "./opencode-runtime"
import { createOpenCodeSessionApi } from "./opencode-sdk"
import { OpenCodeStudioClient } from "./opencode-studio"
import { loadStudioModelConfiguration } from "./model-config"
import { studioMemoryRoot } from "./application-memory"

export async function startOpenCodeStudio(input: {
  command: string
  projectRoot: string
  startupTimeoutMs?: number
  /** Override used by hermetic tests; production defaults outside the project. */
  memoryRoot?: string
}): Promise<{
  client: StudioClient
  close(): Promise<void>
}> {
  const modelConfiguration = await loadStudioModelConfiguration(input.projectRoot)
  const memoryRoot = input.memoryRoot ?? studioMemoryRoot(input.projectRoot)
  const runtime = new OpenCodeRuntime({
    ...input,
    memoryRoot,
    config: modelConfiguration.runtime,
    modelParameters: modelConfiguration.requestParameters,
  })
  const server = await runtime.start()
  const client = new OpenCodeStudioClient(
    createOpenCodeSessionApi({
      baseUrl: server.url,
      directory: input.projectRoot,
      models: modelConfiguration.catalog,
    }),
    { memoryRoot },
  )
  return {
    client,
    async close() {
      try {
        await client.close()
      } finally {
        await server.close()
      }
    },
  }
}
