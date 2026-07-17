import { describe, expect, test } from "bun:test"
import { sortByStatus, statusColor, statusPresentation } from "../../src/ui/status"

describe("AgentLoom status presentation", () => {
  test("maps runtime and result states to semantic colors", () => {
    expect(statusPresentation("failed").tone).toBe("error")
    expect(statusPresentation("crashed").tone).toBe("error")
    expect(statusPresentation("running").tone).toBe("info")
    expect(statusPresentation("completed").tone).toBe("success")
    expect(statusPresentation("available").tone).toBe("success")
    expect(statusPresentation("unavailable").tone).toBe("warning")
    expect(statusPresentation("never_run").tone).toBe("muted")

    expect(statusColor("failed", "dark")).toBe("#e06c75")
    expect(statusColor("completed", "light")).toBe("#3d9a57")
  })

  test("sorts failures and active runs first without mutating the input", () => {
    const input = [
      { id: "never", status: "never_run" },
      { id: "done", status: "completed" },
      { id: "active-a", status: "running" },
      { id: "failed", status: "failed" },
      { id: "available", status: "available" },
      { id: "crashed", status: "crashed" },
      { id: "active-b", status: "running" },
      { id: "unavailable", status: "unavailable" },
    ] as const

    expect(sortByStatus(input, (item) => item.status).map((item) => item.id)).toEqual([
      "crashed",
      "failed",
      "active-a",
      "active-b",
      "unavailable",
      "done",
      "available",
      "never",
    ])
    expect(input.map((item) => item.id)).toEqual([
      "never",
      "done",
      "active-a",
      "failed",
      "available",
      "crashed",
      "active-b",
      "unavailable",
    ])
  })
})
