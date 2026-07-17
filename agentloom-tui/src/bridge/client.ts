import {
  rpcErrorCodes,
  type BootstrapResultDto,
  type RpcErrorCode,
  type RpcMethod,
  type RpcParams,
  type RpcRequest,
  type RpcResult,
  type RunDetailResultDto,
  type SystemDetailResultDto,
} from "../domain/dto"

export interface BridgeTransportHandlers {
  line(line: string): void
  error(error: Error): void
  close(): void
}

export interface BridgeTransport {
  start(handlers: BridgeTransportHandlers): Promise<void>
  send(line: string): Promise<void>
  close(): Promise<void>
}

export class BridgeRpcError extends Error {
  readonly code: RpcErrorCode

  constructor(code: RpcErrorCode, message: string) {
    super(message)
    this.name = "BridgeRpcError"
    this.code = code
  }
}

export class BridgeProtocolError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "BridgeProtocolError"
  }
}

export class BridgeClosedError extends Error {
  constructor(message = "AgentLoom bridge is closed") {
    super(message)
    this.name = "BridgeClosedError"
  }
}

export interface BridgeClientOptions {
  createID?: () => string
}

export class BridgeClient {
  private readonly pending = new Map<
    string,
    { resolve: (result: unknown) => void; reject: (error: Error) => void }
  >()
  private readonly createID: () => string
  private startPromise?: Promise<void>
  private closePromise?: Promise<void>
  private closed = false

  constructor(
    private readonly transport: BridgeTransport,
    options: BridgeClientOptions = {},
  ) {
    this.createID = options.createID ?? (() => crypto.randomUUID())
  }

  bootstrap() {
    return this.request("bootstrap", {})
  }

  systemDetail(systemID: string) {
    return this.request("system.detail", { system_id: systemID })
  }

  runDetail(runID: string, applicationID: string, systemID?: string) {
    return this.request("run.detail", {
      run_id: runID,
      application_id: applicationID,
      ...(systemID ? { system_id: systemID } : {}),
    })
  }

  builderSend(sessionID: string, message: string, modelType?: string) {
    return this.request("builder.send", {
      session_id: sessionID,
      message,
      ...(modelType ? { model_type: modelType } : {}),
    })
  }

  builderDraft(sessionID: string) {
    return this.request("builder.draft", { session_id: sessionID })
  }

  applyDraft(sessionID: string, expectedRevision: number) {
    return this.request("draft.apply", {
      session_id: sessionID,
      expected_revision: expectedRevision,
    })
  }

  async request<Method extends RpcMethod>(method: Method, params: RpcParams<Method>): Promise<RpcResult<Method>> {
    if (this.closed) throw new BridgeClosedError()
    await this.start()
    if (this.closed) throw new BridgeClosedError()

    const id = this.createID()
    if (this.pending.has(id)) throw new BridgeProtocolError(`duplicate request id: ${id}`)
    const request: RpcRequest<Method> = { id, method, params }

    return new Promise<RpcResult<Method>>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (result) => resolve(result as RpcResult<Method>),
        reject,
      })
      this.transport.send(JSON.stringify(request)).catch((error: unknown) => {
        const pending = this.pending.get(id)
        if (!pending) return
        this.pending.delete(id)
        pending.reject(asError(error))
      })
    })
  }

  close() {
    if (this.closePromise) return this.closePromise
    this.closed = true
    this.rejectPending(new BridgeClosedError())
    this.closePromise = this.transport.close()
    return this.closePromise
  }

  private start() {
    if (this.startPromise) return this.startPromise
    this.startPromise = this.transport.start({
      line: (line) => this.receive(line),
      error: (error) => this.fail(error),
      close: () => this.fail(new BridgeClosedError("AgentLoom bridge transport closed")),
    })
    return this.startPromise
  }

  private receive(line: string) {
    const response = parseResponse(line)
    if (response instanceof Error) {
      this.fail(response)
      return
    }

    const pending = this.pending.get(response.id)
    if (!pending) {
      this.fail(new BridgeProtocolError(`response has no pending request: ${response.id}`))
      return
    }
    this.pending.delete(response.id)
    if (response.ok) {
      pending.resolve(response.result)
      return
    }
    pending.reject(new BridgeRpcError(response.error.code, response.error.message))
  }

  private fail(error: Error) {
    if (this.closed) return
    this.closed = true
    this.rejectPending(error)
  }

  private rejectPending(error: Error) {
    for (const pending of this.pending.values()) pending.reject(error)
    this.pending.clear()
  }
}

type ParsedResponse =
  | { id: string; ok: true; result: unknown }
  | { id: string; ok: false; error: { code: RpcErrorCode; message: string } }

function parseResponse(line: string): ParsedResponse | BridgeProtocolError {
  const value = (() => {
    try {
      return JSON.parse(line) as unknown
    } catch {
      return undefined
    }
  })()
  if (!isRecord(value)) return new BridgeProtocolError("bridge response must be one JSON object")
  if (typeof value.id !== "string" || typeof value.ok !== "boolean") {
    return new BridgeProtocolError("bridge response must contain string id and boolean ok")
  }
  if (value.ok) {
    if (!("result" in value)) return new BridgeProtocolError("successful bridge response is missing result")
    return { id: value.id, ok: true, result: value.result }
  }
  if (!isRecord(value.error)) return new BridgeProtocolError("failed bridge response is missing error")
  if (!isRpcErrorCode(value.error.code) || typeof value.error.message !== "string") {
    return new BridgeProtocolError("bridge response contains an invalid RPC error")
  }
  return {
    id: value.id,
    ok: false,
    error: { code: value.error.code, message: value.error.message },
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function isRpcErrorCode(value: unknown): value is RpcErrorCode {
  return typeof value === "string" && rpcErrorCodes.some((code) => code === value)
}

function asError(value: unknown) {
  if (value instanceof Error) return value
  return new Error(String(value))
}

export type { BootstrapResultDto, RunDetailResultDto, SystemDetailResultDto }
