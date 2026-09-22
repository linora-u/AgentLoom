import { describe, expect, test } from "bun:test"
import { selectionKey, type SelectionIdentity } from "../../src/domain/selection"

describe("selection identity", () => {
  test("does not collide when two applications expose the same runtime id", () => {
    const alpha: SelectionIdentity = { kind: "run", applicationID: "alpha", runID: "run-1" }
    const beta: SelectionIdentity = { kind: "run", applicationID: "beta", runID: "run-1" }

    expect(selectionKey(alpha)).not.toBe(selectionKey(beta))
  })

  test("keeps concurrent calls of the same agent independently selectable", () => {
    const first: SelectionIdentity = {
      kind: "worker",
      applicationID: "reports",
      runID: "run-1",
      agentName: "searcher",
      callIndex: 0,
    }
    const second: SelectionIdentity = { ...first, callIndex: 1 }

    expect(selectionKey(first)).not.toBe(selectionKey(second))
    expect(selectionKey(first)).toBe(selectionKey({ ...first }))
  })

  test("uses unambiguous keys even when identifiers contain separators", () => {
    const first: SelectionIdentity = { kind: "system", systemID: "a:b" }
    const second: SelectionIdentity = { kind: "system", systemID: "a" }

    expect(selectionKey(first)).not.toBe(selectionKey(second))
  })
})
