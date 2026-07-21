import { describe, expect, test } from "bun:test"
import type { StudioClient, TuiClient } from "../../src/app/session"
import { runInteractiveStudio } from "../../src/cli/interactive"

describe("interactive Studio composition", () => {
  test("starts OpenCode, injects it into the TUI, and closes it on exit", async () => {
    const events: string[] = []
    const bridge = {} as TuiClient
    const studioClient = {} as StudioClient

    await runInteractiveStudio(
      { bridge, projectRoot: "/repo", openCodeCommand: "/runtime/opencode" },
      {
        startStudio: async (input) => {
          events.push(`start:${input.command}:${input.projectRoot}`)
          return {
            client: studioClient,
            close: async () => { events.push("close") },
          }
        },
        runTui: async (input) => {
          expect(input.client).toBe(bridge)
          expect(input.studio).toBe(studioClient)
          events.push("tui")
        },
      },
    )

    expect(events).toEqual(["start:/runtime/opencode:/repo", "tui", "close"])
  })

  test("still closes OpenCode when the TUI fails", async () => {
    const events: string[] = []

    await expect(runInteractiveStudio(
      { bridge: {} as TuiClient, projectRoot: "/repo", openCodeCommand: "opencode" },
      {
        startStudio: async () => ({
          client: {} as StudioClient,
          close: async () => { events.push("close") },
        }),
        runTui: async () => { throw new Error("renderer failed") },
      },
    )).rejects.toThrow("renderer failed")

    expect(events).toEqual(["close"])
  })

  test("closes the active Runtime before safely restarting the updated product", async () => {
    const events: string[] = []

    await runInteractiveStudio(
      { bridge: {} as TuiClient, projectRoot: "/repo", openCodeCommand: "opencode" },
      {
        startStudio: async () => ({
          client: {} as StudioClient,
          close: async () => { events.push("close") },
        }),
        runTui: async () => {
          events.push("update-installed")
          return "restart"
        },
        restart: (projectRoot) => { events.push(`restart:${projectRoot}`) },
      },
    )

    expect(events).toEqual(["update-installed", "close", "restart:/repo"])
  })
})
