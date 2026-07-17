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

export interface BootstrapResultDto {
  project: ProjectDto
  models: ModelsDto
  systems: SystemSummaryDto[]
  runs: RunSummaryDto[]
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
  "run.detail": {
    params: { run_id: string; application_id: string; system_id?: string }
    result: RunDetailResultDto
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
  "builder_failed",
  "draft_conflict",
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
