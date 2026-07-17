import { describe, expect, test } from "bun:test"
import type { BootstrapResultDto } from "../../src/domain"
import {
  buildSidebarGroups,
  nextSelection,
  parseBuilderInput,
  routeForEntry,
} from "../../src/app/controller"

const bootstrap: BootstrapResultDto = {
  project: { root: "/repo", name: "repo" },
  models: {
    default: "powerful",
    configured: true,
    items: [{ type: "powerful", description: "", default: true, configured: true }],
  },
  systems: [
    {
      id: "applications/new/workflows/new.yaml",
      path: "applications/new/workflows/new.yaml",
      application_id: "new",
      name: "new_agent",
      description: "not run",
      state: "never_run",
      validation: { valid: true, errors: [] },
      latest_run: null,
    },
    {
      id: "applications/live/workflows/live.yaml",
      path: "applications/live/workflows/live.yaml",
      application_id: "live",
      name: "live_agent",
      description: "running",
      state: "running",
      validation: { valid: true, errors: [] },
      latest_run: null,
    },
  ],
  runs: [
    {
      run_id: "run-live",
      system_id: "applications/live/workflows/live.yaml",
      application_id: "live",
      task_id: "task-live",
      agent_name: "live_agent",
      status: "running",
      started_at: "2026-07-17T10:00:00Z",
      ended_at: null,
    },
  ],
}

describe("TUI controller", () => {
  test("keeps never-run definitions and active runs independently clickable", () => {
    const groups = buildSidebarGroups(bootstrap)

    expect(groups.systems.map((entry) => [entry.title, entry.status])).toEqual([
      ["live_agent", "running"],
      ["new_agent", "never_run"],
    ])
    expect(groups.runs.map((entry) => [entry.title, entry.status])).toEqual([["live_agent", "running"]])
    expect(routeForEntry(groups.systems[1]!)).toEqual({
      type: "system",
      systemID: "applications/new/workflows/new.yaml",
    })
    expect(routeForEntry(groups.runs[0]!)).toEqual({
      type: "run",
      runID: "run-live",
      applicationID: "live",
      systemID: "applications/live/workflows/live.yaml",
    })
  })

  test("wraps keyboard selection across every visible Agent and Run", () => {
    const groups = buildSidebarGroups(bootstrap)
    const entries = [...groups.systems, ...groups.runs]

    expect(nextSelection(0, -1, entries.length)).toBe(2)
    expect(nextSelection(2, 1, entries.length)).toBe(0)
    expect(nextSelection(0, 1, 0)).toBe(0)
  })

  test("keeps unlinked same-id runs from different applications independently clickable", () => {
    const groups = buildSidebarGroups({
      ...bootstrap,
      systems: [],
      runs: ["alpha", "beta"].map((applicationID) => ({
        run_id: "shared-run",
        system_id: null,
        application_id: applicationID,
        task_id: `task-${applicationID}`,
        agent_name: applicationID,
        status: "completed" as const,
        started_at: "2026-07-17T10:00:00Z",
        ended_at: "2026-07-17T10:01:00Z",
      })),
    })

    expect(groups.runs.map((entry) => entry.key)).toHaveLength(2)
    expect(groups.runs[0]!.key).not.toBe(groups.runs[1]!.key)
  })

  test("keeps apply and model selection explicit instead of sending them to the model", () => {
    expect(parseBuilderInput("/apply")).toEqual({ type: "apply" })
    expect(parseBuilderInput("/models")).toEqual({ type: "models" })
    expect(parseBuilderInput("/model fast")).toEqual({ type: "model", modelType: "fast" })
    expect(parseBuilderInput("/refresh")).toEqual({ type: "refresh" })
    expect(parseBuilderInput("创建一个总结 Agent")).toEqual({
      type: "send",
      message: "创建一个总结 Agent",
    })
  })
})
