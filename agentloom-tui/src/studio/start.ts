import type { StudioClient } from "../app/session"
import { OpenCodeRuntime } from "./opencode-runtime"
import { createOpenCodeSessionApi } from "./opencode-sdk"
import { OpenCodeStudioClient } from "./opencode-studio"
import { loadStudioModelConfiguration } from "./model-config"

export async function startOpenCodeStudio(input: {
  command: string
  projectRoot: string
  startupTimeoutMs?: number
}): Promise<{
  client: StudioClient
  close(): Promise<void>
}> {
  const modelConfiguration = await loadStudioModelConfiguration(input.projectRoot)
  const runtime = new OpenCodeRuntime({
    ...input,
    config: modelConfiguration.runtime,
    modelParameters: modelConfiguration.requestParameters,
  })
  const server = await runtime.start()
  const client = new OpenCodeStudioClient(createOpenCodeSessionApi({
    baseUrl: server.url,
    directory: input.projectRoot,
    models: modelConfiguration.catalog,
  }))
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
