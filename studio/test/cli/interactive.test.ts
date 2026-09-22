import { describe, expect, test } from "bun:test"
import type { StudioClient, StudioBackend } from "../../src/app/session"
import { runInteractiveStudio } from "../../src/cli/interactive"

describe("interactive Studio composition", () => {
  test("starts OpenCode, injects it into Studio, and closes it on exit", async () => {
    const events: string[] = []
    const backend = {} as StudioBackend
    const studioClient = {} as StudioClient

    await runInteractiveStudio(
      { backend, projectRoot: "/repo", openCodeCommand: "/runtime/opencode" },
      {
        startStudio: async (input) => {
          events.push(`start:${input.command}:${input.projectRoot}`)
          return {
            client: studioClient,
            close: async () => { events.push("close") },
          }
        },
        runStudio: async (input) => {
          expect(input.client).toBe(backend)
          expect(input.studio).toBe(studioClient)
          events.push("studio")
        },
      },
    )

    expect(events).toEqual(["start:/runtime/opencode:/repo", "studio", "close"])
  })

  test("still closes OpenCode when Studio fails", async () => {
    const events: string[] = []

    await expect(runInteractiveStudio(
      { backend: {} as StudioBackend, projectRoot: "/repo", openCodeCommand: "opencode" },
      {
        startStudio: async () => ({
          client: {} as StudioClient,
          close: async () => { events.push("close") },
        }),
        runStudio: async () => { throw new Error("renderer failed") },
      },
    )).rejects.toThrow("renderer failed")

    expect(events).toEqual(["close"])
  })

  test("closes the active Runtime before safely restarting the updated product", async () => {
    const events: string[] = []

    await runInteractiveStudio(
      { backend: {} as StudioBackend, projectRoot: "/repo", openCodeCommand: "opencode" },
      {
        startStudio: async () => ({
          client: {} as StudioClient,
          close: async () => { events.push("close") },
        }),
        runStudio: async () => {
          events.push("update-installed")
          return "restart"
        },
        restart: (projectRoot) => { events.push(`restart:${projectRoot}`) },
      },
    )

    expect(events).toEqual(["update-installed", "close", "restart:/repo"])
  })
})
