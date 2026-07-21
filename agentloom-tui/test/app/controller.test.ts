import { describe, expect, test } from "bun:test"
import type { BootstrapResultDto } from "../../src/domain"
import {
  buildSidebarGroups,
  buildPaletteItems,
  nextSelection,
  parseBuilderInput,
  recentRunEntries,
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
}

const catalogBootstrap: BootstrapResultDto = {
  ...bootstrap,
  worker_invocations: [{
    run_id: "run-live",
    system_id: "applications/live/workflows/live.yaml",
    application_id: "live",
    parent_agent_name: "live_agent",
    agent_name: "researcher",
    call_index: 4,
    status: "running",
    step: 2,
    started_at: "2026-07-17T10:00:10Z",
    ended_at: null,
    error: null,
  }],
  applications: [
    {
      id: "live",
      name: "live",
      path: "applications/live",
      system_count: 1,
      worker_count: 1,
      skill_count: 1,
      run_count: 1,
      active_run_count: 1,
    },
  ],
  agents: [
    {
      id: "applications/live/workflows/live.yaml",
      application_id: "live",
      name: "live_agent",
      description: "running",
      path: "applications/live/workflows/live.yaml",
      role: "supervisor",
      skills: { load_mode: "all", items: ["research"] },
      workers: [
        {
          id: "applications/live/workflows/worker_agents/researcher.yaml",
          application_id: "live",
          name: "researcher",
          description: "collect evidence",
          path: "applications/live/workflows/worker_agents/researcher.yaml",
          role: "worker",
          skills: { load_mode: "selected", items: ["research"] },
          workers: [],
        },
      ],
    },
  ],
  skills: [
    {
      id: "live:research",
      application_id: "live",
      name: "research",
      description: "Research sources",
      origin: "application",
      path: "applications/live/skills/research/SKILL.md",
    },
  ],
  schedules: {
    items: [
      {
        id: "daily-live",
        name: "daily-live",
        enabled: true,
        state: "scheduled",
        yaml_path: "applications/live/workflows/live.yaml",
        trigger: { kind: "cron", expression: "0 9 * * *", timezone: "Asia/Shanghai" },
        next_run_at: "2026-07-18T01:00:00Z",
        last_run_at: null,
        last_status: null,
        run_count: 0,
        last_execution: null,
      },
    ],
    service: {
      ...bootstrap.schedules.service,
      state: "running",
      job_count: 1,
    },
  },
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

  test("builds one searchable command and entity catalog instead of a permanent list", () => {
    const items = buildPaletteItems(bootstrap)
    expect(items).toEqual(expect.arrayContaining([
      expect.objectContaining({ action: "new-application", title: "新建 Application" }),
      expect.objectContaining({ action: "permission-toggle", title: "切换 Full Access" }),
      expect.objectContaining({
        action: "models",
        title: "从 llm.yaml 选择 Studio 模型",
        description: "模型类型、Provider 与认证统一读取项目 config/llm.yaml",
      }),
    ]))

    expect(items.filter((item) => item.category === "Commands").map((item) => item.title)).toEqual([
      "新建 Application",
      "返回对话",
      "刷新工作区",
      "切换 Full Access",
      "从 llm.yaml 选择 Studio 模型",
      "管理定时任务",
    ])
    expect(items.filter((item) => item.category === "Models")).toEqual([])
    expect(items.filter((item) => item.category === "Agents")).toHaveLength(2)
    expect(items.filter((item) => item.category === "Runs")).toHaveLength(1)
  })

  test("projects Applications, Supervisor Agents, Skills, and Schedules without global Worker noise", () => {
    const items = buildPaletteItems(catalogBootstrap)
    const groups = buildSidebarGroups(catalogBootstrap)

    expect(items.filter((item) => item.category === "Applications")).toHaveLength(1)
    expect(items.filter((item) => item.category === "Agents").map((item) => item.title)).toEqual([
      "live_agent",
      "new_agent",
    ])
    expect(items.filter((item) => item.category === "Skills")).toHaveLength(1)
    expect(groups.skills.map((entry) => [entry.title, entry.subtitle])).toEqual([
      ["research", "live"],
    ])
    expect(items.filter((item) => item.category === "Schedules")).toHaveLength(1)
    expect(items.find((item) => item.title === "researcher")).toBeUndefined()
    expect(items.find((item) => item.title === "live_agent")?.description).toContain("主 Agent · 运行中")

    const routes = Object.fromEntries(
      items.flatMap((item) => (
        "entry" in item && item.category !== "Runs"
          ? [[item.title, routeForEntry(item.entry)]]
          : []
      )),
    )
    expect(routes.live).toEqual({ type: "application", applicationID: "live" })
    expect(routes.live_agent).toEqual({
      type: "agent",
      agentID: "applications/live/workflows/live.yaml",
      systemID: "applications/live/workflows/live.yaml",
    })
    expect(routes.research).toEqual({ type: "skill", skillID: "live:research" })
    expect(routes["daily-live"]).toEqual({ type: "schedule", scheduleID: "daily-live" })
    expect(routeForEntry(groups.skills[0]!)).toEqual({ type: "skill", skillID: "live:research" })
  })

  test("keeps nested Application IDs complete and explains their state", () => {
    const applicationID = "memory_feature_validation/variants/on"
    const nestedApplication: BootstrapResultDto = {
      ...bootstrap,
      systems: [{
        ...bootstrap.systems[0]!,
        id: `applications/${applicationID}/workflows/recall.yaml`,
        path: `applications/${applicationID}/workflows/recall.yaml`,
        application_id: applicationID,
        name: "recall",
      }],
      applications: [{
        id: applicationID,
        name: "on",
        path: `applications/${applicationID}`,
        system_count: 1,
        worker_count: 0,
        skill_count: 0,
        run_count: 0,
        active_run_count: 0,
      }],
    }

    const application = buildSidebarGroups(nestedApplication).applications[0]!
    expect(application.title).toBe(applicationID)
    expect(application.subtitle).toBe("配置有效 · 尚未运行")
    expect(buildPaletteItems(nestedApplication)).toContainEqual(expect.objectContaining({
      category: "Applications",
      title: applicationID,
      description: "配置有效 · 尚未运行",
    }))
  })

  test("keeps the severity-sorted directory separate from genuinely recent runs", () => {
    const groups = buildSidebarGroups({
      ...bootstrap,
      systems: [],
      runs: [
        {
          run_id: "run-old-failed",
          system_id: null,
          application_id: "old",
          task_id: "task-old",
          agent_name: "old_agent",
          status: "failed",
          started_at: "2026-07-17T08:00:00Z",
          ended_at: "2026-07-17T08:01:00Z",
        },
        {
          run_id: "run-new-completed",
          system_id: null,
          application_id: "new",
          task_id: "task-new",
          agent_name: "new_agent",
          status: "completed",
          started_at: "2026-07-18T02:00:00Z",
          ended_at: "2026-07-18T02:01:00Z",
        },
        {
          run_id: "run-middle-interrupted",
          system_id: null,
          application_id: "middle",
          task_id: "task-middle",
          agent_name: "middle_agent",
          status: "interrupted",
          started_at: "2026-07-18T09:00:00+08:00",
          ended_at: "2026-07-18T09:01:00+08:00",
        },
      ],
    })

    expect(groups.runs.map((entry) => entry.runID)).toEqual([
      "run-old-failed",
      "run-middle-interrupted",
      "run-new-completed",
    ])
    expect(recentRunEntries(groups.runs).map((entry) => entry.runID)).toEqual([
      "run-new-completed",
      "run-middle-interrupted",
      "run-old-failed",
    ])
  })

  test("never exposes Worker Agents in Ctrl+X even when their runtime projection is incomplete", () => {
    const incomplete = {
      ...catalogBootstrap,
      worker_invocations: [],
      worker_invocations_incomplete: true,
    }

    const agents = buildPaletteItems(incomplete).filter((item) => item.category === "Agents")
    expect(agents.map((item) => item.title)).toEqual(["live_agent", "new_agent"])
    expect(agents.find((item) => item.title === "researcher")).toBeUndefined()
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
    expect(parseBuilderInput("/help")).toEqual({ type: "help" })
    expect(parseBuilderInput("/new")).toEqual({ type: "new" })
    expect(parseBuilderInput("/compact")).toEqual({ type: "compact" })
    expect(parseBuilderInput("/apply")).toEqual({ type: "apply" })
    expect(parseBuilderInput("/models")).toEqual({ type: "models" })
    expect(parseBuilderInput("/model fast")).toEqual({ type: "model", modelType: "fast" })
    expect(parseBuilderInput("/refresh")).toEqual({ type: "refresh" })
    expect(parseBuilderInput("创建一个总结 Agent")).toEqual({
      type: "send",
      message: "创建一个总结 Agent",
    })
  })

  test("parses explicit schedule management commands without sending them to the model", () => {
    expect(parseBuilderInput("/schedule")).toEqual({ type: "schedule.help" })
    expect(parseBuilderInput(
      "/schedule add applications/live/workflows/live.yaml --every 2h --timezone Asia/Shanghai --name \"live digest\"",
    )).toEqual({
      type: "schedule.add",
      yamlPath: "applications/live/workflows/live.yaml",
      name: "live digest",
      schedule: { kind: "interval", every: "2h", timezone: "Asia/Shanghai" },
    })
    expect(parseBuilderInput(
      "/schedule add applications/live/workflows/live.yaml --cron \"0 9 * * *\"",
    )).toEqual({
      type: "schedule.add",
      yamlPath: "applications/live/workflows/live.yaml",
      name: "",
      schedule: { kind: "cron", expression: "0 9 * * *", timezone: "UTC" },
    })
    expect(parseBuilderInput("/schedule pause job_123")).toEqual({
      type: "schedule.mutate",
      action: "pause",
      jobID: "job_123",
    })
    expect(parseBuilderInput("/schedule resume job_123")).toEqual({
      type: "schedule.mutate",
      action: "resume",
      jobID: "job_123",
    })
    expect(parseBuilderInput("/schedule remove job_123")).toEqual({
      type: "schedule.mutate",
      action: "remove",
      jobID: "job_123",
    })
    expect(parseBuilderInput("/schedule add missing.yaml --cron 0 9 * * *")).toEqual({
      type: "invalid",
      message: "cron 表达式包含空格时必须使用引号",
    })
  })
})
