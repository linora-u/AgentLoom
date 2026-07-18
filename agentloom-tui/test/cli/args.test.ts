import { describe, expect, test } from "bun:test"
import { formatSnapshot, parseCliArgs } from "../../src/cli/args"

describe("agentloom CLI", () => {
  test("defaults to the current project and interactive TUI", () => {
    expect(parseCliArgs([], "/workspace")).toEqual({
      projectRoot: "/workspace",
      help: false,
      snapshot: false,
      version: false,
    })
  })

  test("accepts a project path and machine-readable snapshot mode", () => {
    expect(parseCliArgs(["--project", "../repo", "--snapshot"], "/workspace/tui")).toEqual({
      projectRoot: "/workspace/repo",
      help: false,
      snapshot: true,
      version: false,
    })
    expect(parseCliArgs(["--project=./repo"], "/workspace").projectRoot).toBe("/workspace/repo")
  })

  test("rejects unknown options instead of silently changing behavior", () => {
    expect(() => parseCliArgs(["--run"], "/workspace")).toThrow("Unknown option: --run")
  })

  test("snapshot output contains status counts without model secrets", () => {
    const output = JSON.parse(
      formatSnapshot({
        project: { root: "/repo", name: "repo" },
        models: {
          default: "powerful",
          configured: true,
          items: [{ type: "powerful", description: "", default: true, configured: true }],
        },
        systems: [
          {
            id: "a",
            path: "a",
            application_id: "a",
            name: "a",
            description: "",
            state: "never_run",
            validation: { valid: true, errors: [] },
            latest_run: null,
          },
        ],
        runs: [],
        worker_invocations: [],
        worker_invocations_incomplete: false,
        applications: [],
        agents: [],
        skills: [],
        schedules: {
          items: [],
          service: {
            state: "stopped",
            pid: null,
            started_at: null,
            last_tick_at: null,
            last_success_at: null,
            last_error: null,
            job_count: 0,
            due_count: 0,
            claimed_count: 0,
            execution_count: 0,
          },
        },
      }),
    )

    expect(output).toEqual({
      project: { root: "/repo", name: "repo" },
      default_model: "powerful",
      systems: {
        total: 1,
        never_run: 1,
        running: 0,
        completed: 0,
        interrupted: 0,
        failed: 0,
        crashed: 0,
        unknown: 0,
      },
      runs: { total: 0, running: 0, completed: 0, interrupted: 0, failed: 0, crashed: 0, unknown: 0 },
    })
  })
})
