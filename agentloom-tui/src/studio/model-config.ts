import { readFile } from "node:fs/promises"
import { join } from "node:path"
import type { StudioModel } from "../app/session"

type ModelProfile = {
  model?: unknown
  base_url?: unknown
  api_key?: unknown
  max_tokens?: unknown
  temperature?: unknown
  timeout?: unknown
  extra_headers?: unknown
  extra_body?: unknown
}

export type StudioModelConfiguration = {
  runtime: Record<string, unknown>
  catalog: StudioModel[]
  requestParameters: Record<string, { temperature?: number }>
}

export async function loadStudioModelConfiguration(projectRoot: string): Promise<StudioModelConfiguration> {
  const path = join(projectRoot, "config/llm.yaml")
  let raw: unknown
  try {
    raw = Bun.YAML.parse(await readFile(path, "utf8"))
  } catch (error) {
    throw new Error(`无法读取 Studio 模型配置 config/llm.yaml：${safeConfigError(error)}`)
  }
  if (!isRecord(raw) || !isRecord(raw.model)) {
    throw new Error("Studio 模型配置无效：config/llm.yaml 缺少 model 配置")
  }

  const defaultModelType = stringValue(raw.model.default_model_type)
  const profiles = Object.entries(raw.model)
    .filter(([name, value]) => name !== "default_model_type" && name !== "common" && isRecord(value))
    .map(([name, value]) => toProfile(name, value as ModelProfile))
  if (profiles.length === 0) {
    throw new Error("Studio 模型配置无效：config/llm.yaml 没有可用模型类型")
  }
  if (!defaultModelType) {
    throw new Error("Studio 模型配置无效：config/llm.yaml 未设置 model.default_model_type")
  }
  const selected = profiles.find((profile) => profile.type === defaultModelType)
  if (!selected) {
    throw new Error(`Studio 模型配置无效：默认模型类型 ${defaultModelType} 不存在`)
  }

  const providers = Object.fromEntries(profiles.map((profile) => [profile.providerID, {
    id: profile.providerID,
    name: `AgentLoom ${profile.type}`,
    env: [],
    npm: "@ai-sdk/openai-compatible",
    options: {
      ...(profile.apiKey ? { apiKey: profile.apiKey } : {}),
      ...(profile.baseURL ? { baseURL: profile.baseURL } : {}),
      ...(profile.headers ? { headers: profile.headers } : {}),
      ...(profile.timeoutMs ? { timeout: profile.timeoutMs } : {}),
    },
    models: {
      configured: {
        id: profile.wireModel,
        name: profile.type,
        tool_call: true,
        temperature: profile.temperature !== undefined,
        ...(profile.maxTokens ? { limit: { context: 0, output: profile.maxTokens } } : {}),
        ...(profile.options ? { options: profile.options } : {}),
      },
    },
  }]))
  const internalModelID = (type: string) => `${providerID(type)}/configured`
  const smallModelType = profiles.some((profile) => profile.type === "summary") ? "summary" : defaultModelType

  return {
    runtime: {
      formatter: false,
      lsp: false,
      model: internalModelID(defaultModelType),
      small_model: internalModelID(smallModelType),
      enabled_providers: profiles.map((profile) => profile.providerID),
      provider: providers,
    },
    catalog: profiles.map((profile) => ({
      id: profile.type,
      providerID: profile.providerID,
      modelID: "configured",
      name: profile.type,
      providerName: "config/llm.yaml",
      default: profile.type === defaultModelType,
    })),
    requestParameters: Object.fromEntries(profiles.map((profile) => [
      profile.providerID,
      {
        ...(profile.temperature !== undefined ? { temperature: profile.temperature } : {}),
      },
    ])),
  }
}

function toProfile(type: string, value: ModelProfile) {
  const rawModel = stringValue(value.model)
  if (!rawModel) throw new Error(`Studio 模型配置无效：模型类型 ${type} 缺少 model`)
  const configuredBaseURL = stringValue(value.base_url).replace(/\/+$/, "")
  const baseURL = configuredBaseURL || (rawModel.startsWith("openai/") ? "https://api.openai.com/v1" : "")
  const apiKey = stringValue(value.api_key)
  const wireModel = rawModel.replace(/^openai\//, "")
  const maxTokens = positiveInteger(value.max_tokens)
  const timeoutSeconds = positiveNumber(value.timeout)
  const headers = stringRecord(value.extra_headers)
  const extraBody = plainRecord(value.extra_body)
  return {
    type,
    providerID: providerID(type),
    wireModel,
    baseURL,
    apiKey,
    maxTokens,
    timeoutMs: timeoutSeconds ? timeoutSeconds * 1_000 : undefined,
    temperature: finiteNumber(value.temperature),
    headers,
    options: extraBody,
  }
}

function providerID(type: string): string {
  return `agentloom-${Buffer.from(type, "utf8").toString("base64url")}`
}

function safeConfigError(error: unknown): string {
  if (error instanceof SyntaxError) return "YAML 语法错误"
  if (error instanceof Error && "code" in error && error.code === "ENOENT") return "文件不存在"
  return "无法解析文件"
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function positiveNumber(value: unknown): number | undefined {
  const number = finiteNumber(value)
  return number !== undefined && number > 0 ? number : undefined
}

function positiveInteger(value: unknown): number | undefined {
  const number = positiveNumber(value)
  return number !== undefined ? Math.floor(number) : undefined
}

function plainRecord(value: unknown): Record<string, unknown> | undefined {
  return isRecord(value) ? value : undefined
}

function stringRecord(value: unknown): Record<string, string> | undefined {
  if (!isRecord(value)) return undefined
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, String(item)]))
}
