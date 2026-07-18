import { describe, expect, test } from "bun:test"
import { builderSessionStatus, isProblemRuntimeStatus, runtimeStatus } from "../../src/domain/status"

describe("status domains", () => {
  test("keeps builder activity separate from runtime outcomes", () => {
    expect(builderSessionStatus("busy")).toBe("busy")
    expect(builderSessionStatus("running")).toBe("idle")

    expect(runtimeStatus("running")).toBe("running")
    expect(runtimeStatus("retry")).toBe("unknown")
  })

  test("normalizes Python runtime terminal states without inventing builder states", () => {
    expect(runtimeStatus("completed")).toBe("completed")
    expect(runtimeStatus("failed")).toBe("failed")
    expect(runtimeStatus("crashed")).toBe("crashed")
    expect(runtimeStatus("cancelled")).toBe("interrupted")
    expect(runtimeStatus("canceled")).toBe("interrupted")
    expect(runtimeStatus("in_progress")).toBe("running")
    expect(runtimeStatus("never_run")).toBe("never_run")
    expect(runtimeStatus("not-a-runtime-state")).toBe("unknown")
    expect(isProblemRuntimeStatus("unknown-provider-state")).toBe(true)
    expect(isProblemRuntimeStatus("completed")).toBe(false)
  })
})
