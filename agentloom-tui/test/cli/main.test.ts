import { describe, expect, test } from "bun:test"
import { join } from "node:path"
import { formatBridgeFailure, HELP } from "../../src/cli/main"

describe("AgentLoom CLI diagnostics", () => {
  test("help points Ctrl+X Studio model selection at config/llm.yaml", () => {
    expect(HELP).toContain("/models             Select a Studio model from config/llm.yaml")
    expect(HELP).toContain("/compact            Compact the current Session context")
    expect(HELP).toContain("Ctrl-X              Commands, permissions, Applications, main Agents, Runs and Skills")
    expect(HELP).not.toContain("Ctrl-P")
    expect(HELP).not.toContain("OpenCode Providers")
  })

  test("non-interactive CLI modes do not preload the terminal renderer", async () => {
    const repositoryRoot = join(import.meta.dir, "../..")
    const entry = await Bun.file(join(repositoryRoot, "bin/agentloom")).text()
    const main = await Bun.file(join(repositoryRoot, "src/cli/main.ts")).text()

    expect(entry).not.toContain("@opentui/solid/preload")
    expect(main).not.toMatch(/^import .*runTui/m)
    expect(main).toContain('await import("@opentui/solid/preload")')
    expect(main.indexOf('if (args.snapshot)')).toBeLessThan(
      main.indexOf('await import("@opentui/solid/preload")'),
    )
  })

  test("keeps the concise error when the Python bridge has no stderr", () => {
    expect(formatBridgeFailure("Python bridge exited with code 1", [])).toBe(
      "agentloom: Python bridge exited with code 1\n",
    )
  })

  test("shows bounded sanitized Python diagnostics on startup failure", () => {
    const output = formatBridgeFailure(
      "Python bridge exited with code 1",
      ["\u001b[31mTraceback\u001b[0m\n", "x".repeat(6_000), "\nImportError: missing runtime\n"],
    )

    expect(output).toContain("Python bridge diagnostics:")
    expect(output).toContain("ImportError: missing runtime")
    expect(output).not.toContain("\u001b[31m")
    expect(output).not.toContain("x".repeat(5_000))
    expect(output.length).toBeLessThan(4_500)
  })
})
