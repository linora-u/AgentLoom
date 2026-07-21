import type { StudioClient } from "../app/session"
import { OpenCodeRuntime } from "./opencode-runtime"
import { createOpenCodeSessionApi } from "./opencode-sdk"
import { OpenCodeStudioClient } from "./opencode-studio"

export async function startOpenCodeStudio(input: {
  command: string
  projectRoot: string
  startupTimeoutMs?: number
}): Promise<{
  client: StudioClient
  close(): Promise<void>
}> {
  const runtime = new OpenCodeRuntime(input)
  const server = await runtime.start()
  const client = new OpenCodeStudioClient(createOpenCodeSessionApi({
    baseUrl: server.url,
    directory: input.projectRoot,
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
