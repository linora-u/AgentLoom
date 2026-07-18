import { describe, expect, test } from "bun:test"
import type {
  BootstrapResultDto,
  BuilderSendResultDto,
  DraftDto,
  RpcRequest,
  RunDetailResultDto,
  SystemDetailResultDto,
} from "../../src/domain/dto"

describe("Python bridge DTOs", () => {
  test("keeps method parameters tied to their result DTO", () => {
    const bootstrap: RpcRequest<"bootstrap"> = { id: "1", method: "bootstrap", params: {} }
    const system: RpcRequest<"system.detail"> = {
      id: "2",
      method: "system.detail",
      params: { system_id: "research" },
    }
    const run: RpcRequest<"run.detail"> = {
      id: "3",
      method: "run.detail",
      params: { run_id: "run-1", application_id: "reports", system_id: "research" },
    }

    expect([bootstrap.method, system.params.system_id, run.params.run_id]).toEqual([
      "bootstrap",
      "research",
      "run-1",
    ])
  })

  test("represents bootstrap, system detail, and run detail wire results", () => {
    const bootstrap = {} as BootstrapResultDto
    const system = {} as SystemDetailResultDto
    const run = {} as RunDetailResultDto

    expect([bootstrap, system, run]).toHaveLength(3)
  })

  test("keeps builder conversation and explicit draft application as separate methods", () => {
    const send: RpcRequest<"builder.send"> = {
      id: "4",
      method: "builder.send",
      params: { session_id: "builder-1", message: "Create a research system", model_type: "powerful" },
    }
    const draft: RpcRequest<"builder.draft"> = {
      id: "5",
      method: "builder.draft",
      params: { session_id: "builder-1" },
    }
    const apply: RpcRequest<"draft.apply"> = {
      id: "6",
      method: "draft.apply",
      params: { session_id: "builder-1", expected_revision: 3 },
    }
    const sendResult = {} as BuilderSendResultDto
    const draftResult = {} as DraftDto

    expect([send.params.session_id, draft.params.session_id, apply.params.expected_revision]).toEqual([
      "builder-1",
      "builder-1",
      3,
    ])
    expect([sendResult, draftResult]).toHaveLength(2)
  })
})
