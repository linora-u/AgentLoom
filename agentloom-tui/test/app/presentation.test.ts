import { describe, expect, test } from "bun:test"
import type { BootstrapResultDto, RunDetailResultDto, SystemDetailResultDto } from "../../src/domain"
import {
  runDetailSections,
  systemDetailSections,
  workspaceEntityDetail,
} from "../../src/app/presentation"

const completeLimits: RunDetailResultDto["limits"] = {
  workers: { truncated: false, returned_count: 1, max_count: 256 },
  events: {
    truncated: false,
    source_incomplete: false,
    returned_count: 1,
    returned_bytes: 64,
    max_count: 200,
    max_bytes: 262_144,
    max_scan_bytes: 524_288,
  },
  logs: {
    truncated: false,
    returned_count: 1,
    returned_bytes: 15,
    max_count: 16,
    max_bytes: 262_144,
    max_bytes_per_file: 32_768,
    max_scanned_entries: 2_048,
  },
  artifacts: {
    truncated: false,
    returned_count: 1,
    max_count: 256,
    max_scanned_entries: 2_048,
  },
  result: { truncated: false, source_incomplete: false, returned_bytes: 12, max_bytes: 131_072 },
}

describe("detail presentation", () => {
  test("workspace entity details preserve application, worker, skill, and schedule context", () => {
    const snapshot: BootstrapResultDto = {
      project: { root: "/repo", name: "repo" },
      models: { default: null, configured: false, items: [] },
      systems: [{
        id: "applications/digest/workflows/digest.yaml",
        path: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        name: "digest_agent",
        description: "Summarize",
        state: "running",
        validation: { valid: true, errors: [] },
        latest_run: {
          run_id: "run-1",
          system_id: "applications/digest/workflows/digest.yaml",
          application_id: "digest",
          task_id: "task-1",
          agent_name: "digest_agent",
          status: "running",
          started_at: "2026-07-18T10:00:00Z",
          ended_at: null,
        },
      }],
      runs: [],
      worker_invocations: [{
        run_id: "run-1",
        system_id: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        parent_agent_name: "digest_agent",
        agent_name: "reader",
        call_index: 3,
        status: "failed",
        step: 4,
        started_at: "2026-07-18T10:00:10Z",
        ended_at: "2026-07-18T10:00:20Z",
        error: "source unavailable",
      }],
      worker_invocations_incomplete: false,
      applications: [{
        id: "digest",
        name: "digest",
        path: "applications/digest",
        system_count: 1,
        worker_count: 1,
        skill_count: 1,
        run_count: 1,
        active_run_count: 1,
      }],
      agents: [{
        id: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        name: "digest_agent",
        description: "Summarize",
        path: "applications/digest/workflows/digest.yaml",
        role: "supervisor",
        skills: { load_mode: "all", items: [] },
        workers: [{
          id: "applications/digest/workflows/worker_agents/reader.yaml",
          application_id: "digest",
          name: "reader",
          description: "Read source files",
          path: "applications/digest/workflows/worker_agents/reader.yaml",
          role: "worker",
          skills: { load_mode: "selected", items: ["pdf"] },
          workers: [],
        }],
      }],
      skills: [{
        id: "digest:pdf",
        application_id: "digest",
        name: "pdf",
        description: "Read PDFs",
        origin: "application",
        path: "applications/digest/skills/pdf/SKILL.md",
      }],
      schedules: {
        items: [{
          id: "daily-digest",
          name: "daily-digest",
          enabled: true,
          state: "scheduled",
          yaml_path: "applications/digest/workflows/digest.yaml",
          trigger: { kind: "cron", expression: "0 9 * * *", timezone: "Asia/Shanghai" },
          next_run_at: "2026-07-19T01:00:00Z",
          last_run_at: "2026-07-18T01:00:00Z",
          last_status: "completed",
          run_count: 3,
          last_execution: null,
        }],
        service: {
          state: "running",
          pid: 42,
          started_at: "2026-07-18T00:00:00Z",
          last_tick_at: "2026-07-18T10:00:00Z",
          last_success_at: "2026-07-18T10:00:00Z",
          last_error: null,
          job_count: 1,
          due_count: 0,
          claimed_count: 0,
          execution_count: 3,
        },
      },
    }

    const application = workspaceEntityDetail(snapshot, { type: "application", applicationID: "digest" })!
    const worker = workspaceEntityDetail(snapshot, {
      type: "agent",
      agentID: "applications/digest/workflows/worker_agents/reader.yaml",
      systemID: "applications/digest/workflows/digest.yaml",
    })!
    const skill = workspaceEntityDetail(snapshot, { type: "skill", skillID: "digest:pdf" })!
    const schedule = workspaceEntityDetail(snapshot, { type: "schedule", scheduleID: "daily-digest" })!

    expect(application.sections.flatMap((section) => section.lines).join("\n")).toContain("reader (worker)")
    const workerText = worker.sections.flatMap((section) => section.lines).join("\n")
    expect(workerText).toContain("Agent 状态: failed")
    expect(workerText).toContain("Run: run-1")
    expect(workerText).toContain("调用: #3")
    expect(workerText).toContain("Step: 4")
    expect(workerText).toContain("错误: source unavailable")
    expect(skill.sections.flatMap((section) => section.lines).join("\n")).toContain("applications/digest/skills/pdf/SKILL.md")
    expect(schedule.sections.flatMap((section) => section.lines).join("\n")).toContain("cron 0 9 * * *")
    expect(schedule.sections.flatMap((section) => section.lines).join("\n")).toContain("调度服务: running")

    const incompleteSnapshot = {
      ...snapshot,
      worker_invocations: [],
      worker_invocations_incomplete: true,
    }
    const incompleteWorker = workspaceEntityDetail(incompleteSnapshot, {
      type: "agent",
      agentID: "applications/digest/workflows/worker_agents/reader.yaml",
      systemID: "applications/digest/workflows/digest.yaml",
    })!
    const incompleteText = incompleteWorker.sections.flatMap((section) => section.lines).join("\n")
    expect(incompleteText).toContain("Agent 状态: incomplete")
    expect(incompleteText).not.toContain("Agent 状态: never_run")
  })

  test("never-run systems show definitions without inventing execution results", () => {
    const detail: SystemDetailResultDto = {
      summary: {
        id: "applications/digest/workflows/digest.yaml",
        path: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        name: "digest_agent",
        description: "Summarize documents",
        state: "never_run",
        validation: { valid: true, errors: [] },
        latest_run: null,
      },
      definition: {
        name: "digest_agent",
        description: "Summarize documents",
        workflow: ["collect", "summarize"],
        model_type: "powerful",
        path: "applications/digest/workflows/digest.yaml",
      },
      files: [{ path: "applications/digest/workflows/digest.yaml", kind: "workflow", size: 320 }],
      topology: {
        supervisor: { name: "digest_agent", path: "applications/digest/workflows/digest.yaml" },
        workers: [{ name: "reader", path: "applications/digest/worker_agents/reader.yaml", description: "Reads" }],
      },
      execution: { state: "never_run", latest_run: null },
      result_state: "never_run",
    }

    const sections = systemDetailSections(detail)
    const text = sections.flatMap((section) => section.lines).join("\n")

    expect(text).toContain("尚未运行，无执行结果")
    expect(text).toContain("模型: powerful")
    expect(text).toContain("工作流: collect → summarize")
    expect(text).toContain("reader — Reads")
    expect(text).toContain("applications/digest/workflows/digest.yaml (workflow, 320 B)")
    expect(text).not.toContain("undefined")
  })

  test("run details expose workers, events, logs, artifacts, and the real result", () => {
    const detail: RunDetailResultDto = {
      summary: {
        run_id: "run-1",
        system_id: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        task_id: "task-1",
        agent_name: "digest_agent",
        status: "completed",
        started_at: "2026-07-17T10:00:00Z",
        ended_at: "2026-07-17T10:01:00Z",
      },
      error: null,
      workers: [
        {
          agent_name: "reader",
          call_index: 0,
          status: "completed",
          step: 2,
          started_at: "2026-07-17T10:00:05Z",
          ended_at: "2026-07-17T10:00:20Z",
          error: null,
        },
      ],
      events: [{ type: "worker.finished", agent_name: "reader" }],
      logs: [
        {
          path: ".agentloom/runs/run-1/run.log",
          size: 90,
          tail: "worker finished",
          tail_truncated: false,
        },
      ],
      artifacts: [{ path: ".agentloom/runs/run-1/result.md", size: 42 }],
      result_state: "available",
      result: "Final digest",
      limits: completeLimits,
    }

    const sections = runDetailSections(detail)
    const text = sections.flatMap((section) => section.lines).join("\n")

    expect(text).toContain("reader #0 — completed — step 2")
    expect(text).toContain('{"type":"worker.finished","agent_name":"reader"}')
    expect(text).toContain(".agentloom/runs/run-1/run.log (90 B)")
    expect(text).toContain("worker finished")
    expect(text).toContain(".agentloom/runs/run-1/result.md (42 B)")
    expect(text).toContain("Final digest")
  })

  test("failed run details show the canonical failure reason", () => {
    const detail: RunDetailResultDto = {
      summary: {
        run_id: "run-failed",
        system_id: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        task_id: "task-failed",
        agent_name: "digest_agent",
        status: "failed",
        started_at: "2026-07-17T10:00:00Z",
        ended_at: "2026-07-17T10:00:05Z",
      },
      error: "provider timed out after 30 seconds",
      workers: [],
      events: [],
      logs: [],
      artifacts: [],
      result_state: "unavailable",
      result: null,
      limits: {
        ...completeLimits,
        workers: { ...completeLimits.workers, returned_count: 0 },
        events: { ...completeLimits.events, returned_count: 0, returned_bytes: 0 },
        logs: { ...completeLimits.logs, returned_count: 0, returned_bytes: 0 },
        artifacts: { ...completeLimits.artifacts, returned_count: 0 },
        result: { ...completeLimits.result, returned_bytes: 0 },
      },
    }

    const text = runDetailSections(detail).flatMap((section) => section.lines).join("\n")

    expect(text).toContain("错误: provider timed out after 30 seconds")
  })

  test("run details label every bounded section instead of implying partial data is complete", () => {
    const detail: RunDetailResultDto = {
      summary: {
        run_id: "run-large",
        system_id: "applications/digest/workflows/digest.yaml",
        application_id: "digest",
        task_id: "task-large",
        agent_name: "digest_agent",
        status: "completed",
        started_at: null,
        ended_at: null,
      },
      error: null,
      workers: [],
      events: [{ type: "task_status_changed", status: "completed" }],
      logs: [{ path: "logs/runtime.log", size: 90_000, tail: "last line", tail_truncated: true }],
      artifacts: [{ path: "artifacts/result.txt", size: 200_000 }],
      result_state: "available",
      result: "partial result",
      limits: {
        ...completeLimits,
        events: { ...completeLimits.events, truncated: true },
        logs: { ...completeLimits.logs, truncated: true },
        artifacts: { ...completeLimits.artifacts, truncated: true },
        result: { ...completeLimits.result, truncated: true },
      },
    }

    const text = runDetailSections(detail).flatMap((section) => section.lines).join("\n")

    expect(text).toContain("事件已截断")
    expect(text).toContain("日志已截断")
    expect(text).toContain("文件列表已截断")
    expect(text).toContain("结果已截断")
    expect(text).toContain("日志尾部已截断")
  })

  test("an incomplete event source never claims that a completed run had no result", () => {
    const detail: RunDetailResultDto = {
      summary: {
        run_id: "run-legacy",
        system_id: null,
        application_id: "legacy",
        task_id: "task-legacy",
        agent_name: "legacy_agent",
        status: "completed",
        started_at: null,
        ended_at: null,
      },
      error: null,
      workers: [],
      events: [],
      logs: [],
      artifacts: [],
      result_state: "unavailable",
      result: null,
      limits: {
        ...completeLimits,
        result: {
          ...completeLimits.result,
          truncated: true,
          source_incomplete: true,
          returned_bytes: 0,
        },
      },
    }

    const text = runDetailSections(detail).flatMap((section) => section.lines).join("\n")

    expect(text).toContain("事件源已截断，无法确认")
    expect(text).not.toContain("未保留可读取的结果")
  })
})
