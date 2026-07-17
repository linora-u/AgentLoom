import { describe, expect, test } from "bun:test"
import { formatBridgeFailure } from "../../src/cli/main"

describe("AgentLoom CLI diagnostics", () => {
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
