import type { RuntimeStatus } from "./status"

export type RunStatus = Exclude<RuntimeStatus, "never_run">
export type ResultState = "never_run" | "running" | "available" | "unavailable"

export interface ProjectDto {
  root: string
  name: string
}

export interface ModelItemDto {
  type: string
  description: string
  default: boolean
  configured: boolean
}

export interface ModelsDto {
  default: string | null
  configured: boolean
  items: ModelItemDto[]
}

export interface ValidationDto {
  valid: boolean
  errors: string[]
}

export interface RunSummaryDto {
  run_id: string
  system_id: string | null
  application_id: string
  task_id: string | null
  agent_name: string
  status: RunStatus
  started_at: string | null
  ended_at: string | null
}

export interface SystemSummaryDto {
  id: string
  path: string
  application_id: string
  name: string
  description: string
  state: RuntimeStatus
  validation: ValidationDto
  latest_run: RunSummaryDto | null
}

export interface ApplicationSummaryDto {
  id: string
  name: string
  path: string
  system_count: number
  worker_count: number
  skill_count: number
  run_count: number
  active_run_count: number
}

export type CapabilitySource = "global" | "application" | "agent" | "none"

export interface SourcedCapabilityDto {
  value: unknown
  source: CapabilitySource
  source_path: string | null
}

export interface EffectiveSkillDto {
  name: string
  description: string
  source: Exclude<CapabilitySource, "none">
  load_mode: string
  path: string
}

export interface EffectiveAgentDetailDto {
  id: string
  name: string
  description: string
  role: "supervisor" | "worker"
  workflow: string
  model: { type: string; source: "global" | "agent" }
  tools: Array<{ name: string; source: "agent" }>
  skills: EffectiveSkillDto[]
  permissions: SourcedCapabilityDto
  hooks: SourcedCapabilityDto
  mcp: SourcedCapabilityDto
  source_path: string
  validation: ValidationDto
  workers: EffectiveAgentDetailDto[]
}

export interface ApplicationDetailResultDto {
  schema_version: 1
  application: {
    id: string
    name: string
    path: string
    health: "healthy" | "invalid"
    updated_at: string | null
  }
  working_revision: string
  running_revision: string | null
  agents: EffectiveAgentDetailDto[]
}

export interface ConfiguredSkillsDto {
  load_mode: string | null
  items: string[]
}

export interface AgentCatalogDto {
  id: string
  application_id: string
  name: string
  description: string
  path: string
  role: "supervisor" | "worker"
  skills: ConfiguredSkillsDto
  workers: AgentCatalogDto[]
}

export interface SkillSummaryDto {
  id: string
  application_id: string | null
  name: string
  description: string
  origin: "global" | "application" | "agent"
  path: string
}

export type ScheduleTriggerDto =
  | { kind: "once"; at: string | null; timezone: string }
  | { kind: "interval"; seconds: number; timezone: string }
  | { kind: "cron"; expression: string; timezone: string }
  | { kind: "unknown" }

export interface ScheduleExecutionSummaryDto {
  id: string
  job_id: string
  status: string
  trigger: string | null
  claimed_at: string | null
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  error: string | null
}

export interface ScheduleSummaryDto {
  id: string
  name: string
  enabled: boolean
  state: string
  yaml_path: string | null
  trigger: ScheduleTriggerDto
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  run_count: number
  last_execution: ScheduleExecutionSummaryDto | null
}

export interface ScheduleServiceDto {
  state: "running" | "stale" | "stopped" | "error"
  pid: number | null
  started_at: string | null
  last_tick_at: string | null
  last_success_at: string | null
  last_error: string | null
  job_count: number
  due_count: number
  claimed_count: number
  execution_count: number
}

export type ScheduleInputDto =
  | { kind: "once"; at: string; timezone: string }
  | { kind: "interval"; every: string; timezone: string }
  | { kind: "cron"; expression: string; timezone: string }

export interface ScheduleMutationResultDto {
  action: "add" | "pause" | "resume" | "remove"
  job_id: string
  name: string
  state: string
}

export interface BootstrapResultDto {
  project: ProjectDto
  models: ModelsDto
  systems: SystemSummaryDto[]
  runs: RunSummaryDto[]
  worker_invocations: WorkerInvocationSummaryDto[]
  worker_invocations_incomplete: boolean
  applications: ApplicationSummaryDto[]
  agents: AgentCatalogDto[]
  skills: SkillSummaryDto[]
  schedules: {
    items: ScheduleSummaryDto[]
    service: ScheduleServiceDto
  }
}

export interface RuntimeSummaryDto {
  systems: SystemSummaryDto[]
  runs: RunSummaryDto[]
  runs_incomplete: boolean
  removed_runs: Array<{ application_id: string; run_id: string }>
  worker_invocations: WorkerInvocationSummaryDto[]
  worker_invocations_incomplete: boolean
  schedules: {
    items: ScheduleSummaryDto[]
    service: ScheduleServiceDto
  }
}

export interface AgentDefinitionDto {
  name: string
  description: string
  workflow: string | string[]
  model_type: string | null
  path: string
}

export interface SystemFileDto {
  path: string
  kind: string
  size: number
}

export interface TopologyAgentDto {
  name: string
  path: string
}

export interface TopologyWorkerDto extends TopologyAgentDto {
  description: string
}

export interface SystemDetailResultDto {
  summary: SystemSummaryDto
  definition: AgentDefinitionDto
  files: SystemFileDto[]
  topology: {
    supervisor: TopologyAgentDto
    workers: TopologyWorkerDto[]
  }
  execution: {
    state: RuntimeStatus
    latest_run: RunSummaryDto | null
  }
  result_state: ResultState
}

export interface RunWorkerDto {
  agent_name: string
  call_index: number
  status: string
  step: number | null
  started_at: string | null
  ended_at: string | null
  error: string | null
}

export interface WorkerInvocationSummaryDto extends RunWorkerDto {
  run_id: string
  system_id: string
  application_id: string
  parent_agent_name: string
}

export interface RunLogDto {
  path: string
  size: number
  tail: string
  tail_truncated: boolean
}

export interface RunArtifactDto {
  path: string
  size: number
}

export interface RunCountLimitDto {
  truncated: boolean
  returned_count: number
  max_count: number
}

export interface RunEventLimitDto extends RunCountLimitDto {
  source_incomplete: boolean
  returned_bytes: number
  max_bytes: number
  max_scan_bytes: number
}

export interface RunLogLimitDto extends RunCountLimitDto {
  returned_bytes: number
  max_bytes: number
  max_bytes_per_file: number
  max_scanned_entries: number
}

export interface RunArtifactLimitDto extends RunCountLimitDto {
  max_scanned_entries: number
}

export interface RunResultLimitDto {
  truncated: boolean
  source_incomplete: boolean
  returned_bytes: number
  max_bytes: number
}

export interface RunDetailLimitsDto {
  workers: RunCountLimitDto
  events: RunEventLimitDto
  logs: RunLogLimitDto
  artifacts: RunArtifactLimitDto
  result: RunResultLimitDto
}

export interface RunDetailResultDto {
  summary: RunSummaryDto
  error: string | null
  workers: RunWorkerDto[]
  events: Array<Record<string, unknown>>
  logs: RunLogDto[]
  artifacts: RunArtifactDto[]
  result_state: ResultState
  result: string | null
  limits: RunDetailLimitsDto
}

export interface DraftFileDto {
  path: string
  change: "create" | "modify"
  content: string
}

export interface DraftDto {
  revision: number
  valid: boolean
  errors: string[]
  files: DraftFileDto[]
}

export interface BuilderSendResultDto {
  session_id: string
  assistant: string
  model_type: string | null
  draft: DraftDto
}

export interface DraftApplyResultDto {
  applied: boolean
  revision: number
  files: string[]
}

export interface RpcMethods {
  bootstrap: {
    params: Record<string, never>
    result: BootstrapResultDto
  }
  "system.detail": {
    params: { system_id: string }
    result: SystemDetailResultDto
  }
  "application.detail": {
    params: { application_id: string }
    result: ApplicationDetailResultDto
  }
  "run.detail": {
    params: { run_id: string; application_id: string; system_id?: string }
    result: RunDetailResultDto
  }
  "runtime.summary": {
    params: Record<string, never>
    result: RuntimeSummaryDto
  }
  "schedule.add": {
    params: { yaml_path: string; name: string; schedule: ScheduleInputDto }
    result: ScheduleMutationResultDto
  }
  "schedule.pause": {
    params: { job_id: string }
    result: ScheduleMutationResultDto
  }
  "schedule.resume": {
    params: { job_id: string }
    result: ScheduleMutationResultDto
  }
  "schedule.remove": {
    params: { job_id: string }
    result: ScheduleMutationResultDto
  }
  "assistant.send": {
    params: { session_id: string; message: string; model_type?: string }
    result: BuilderSendResultDto
  }
  "builder.send": {
    params: { session_id: string; message: string; model_type?: string }
    result: BuilderSendResultDto
  }
  "builder.draft": {
    params: { session_id: string }
    result: DraftDto
  }
  "draft.apply": {
    params: { session_id: string; expected_revision: number }
    result: DraftApplyResultDto
  }
}

export type RpcMethod = keyof RpcMethods
export type RpcParams<Method extends RpcMethod> = RpcMethods[Method]["params"]
export type RpcResult<Method extends RpcMethod> = RpcMethods[Method]["result"]

export interface RpcRequest<Method extends RpcMethod = RpcMethod> {
  id: string
  method: Method
  params: RpcParams<Method>
}

export const rpcErrorCodes = [
  "invalid_request",
  "method_not_found",
  "invalid_params",
  "not_found",
  "not_ready",
  "builder_failed",
  "assistant_config",
  "assistant_timeout",
  "assistant_rate_limit",
  "assistant_auth",
  "assistant_bad_request",
  "assistant_unavailable",
  "assistant_provider_error",
  "assistant_protocol",
  "assistant_failed",
  "assistant_tool_limit",
  "draft_conflict",
  "schedule_failed",
  "busy",
  "internal_error",
] as const

export type RpcErrorCode = (typeof rpcErrorCodes)[number]

export interface RpcErrorDto {
  code: RpcErrorCode
  message: string
}

export type RpcResponse<Result = unknown> =
  | { id: string; ok: true; result: Result }
  | { id: string; ok: false; error: RpcErrorDto }

export type AssistantTurnEventDto =
  | { request_id: string; session_id: string; type: "turn.started" }
  | { request_id: string; session_id: string; type: "turn.delta"; text: string }
  | {
      request_id: string
      session_id: string
      type: "turn.activity"
      state: "started" | "completed"
      name: string
    }
  | { request_id: string; session_id: string; type: "turn.completed" }

export interface RpcEventEnvelopeDto {
  event: AssistantTurnEventDto
}
