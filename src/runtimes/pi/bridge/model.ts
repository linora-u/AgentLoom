/** AgentLoom permissions and evidence around the native SDK stream; no request policy. */
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import * as zlib from "node:zlib";
import { createAssistantMessageEventStream, type AssistantMessage, type Model, type Api } from "@earendil-works/pi-ai";
import type { AgentSession } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;

const MAX_MODEL_CAPTURE_BYTES = 32 * 1024 * 1024;
export type SearchEvidence = {calls: {id: string; status: "completed"; action: Obj | null;
  action_url_sha256?: string}[];
  citations: {url: string; title: string}[]; serviceTier?: string};
async function readBounded(body: ReadableStream<Uint8Array> | null): Promise<Buffer> {
  if (!body) return Buffer.alloc(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_MODEL_CAPTURE_BYTES) {
      void reader.cancel().catch(() => {});
      throw new Error("Pi Model HTTP capture exceeds the 32 MiB limit");
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks, size);
}

function searchEvidence(events: readonly unknown[]): SearchEvidence {
  const calls = new Map<string, SearchEvidence["calls"][number]>();
  const citations = new Map<string, string>();
  let serviceTier: string | undefined;
  const inspect = (value: unknown): void => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) {for (const part of value) inspect(part); return;}
    const item = value as Obj;
    if (item.type === "web_search_call" && item.status === "completed") {
      const id = typeof item.id === "string" ? item.id : `search-${calls.size + 1}`;
      const action = item.action && typeof item.action === "object" && !Array.isArray(item.action)
        ? item.action as Obj : null;
      calls.set(id, {id, status: "completed", action,
        ...(typeof action?.url === "string" ? {action_url_sha256:
          createHash("sha256").update(action.url).digest("hex")} : {})});
    }
    if (item.type === "url_citation" && typeof item.url === "string") {
      try {
        const url = new URL(item.url);
        if (url.protocol === "https:" || url.protocol === "http:")
          citations.set(url.href, typeof item.title === "string" && item.title.trim()
            ? item.title.trim().replace(/[\r\n]/g, " ") : url.hostname);
      } catch { /* Ignore invalid provider citations. */ }
    }
    for (const part of Object.values(item)) inspect(part);
  };
  for (const event of events) {
    const value = event as Obj;
    if (value.type === "response.output_item.done") inspect(value.item);
    else if (["response.completed", "response.incomplete", "response.failed"].includes(value.type)) {
      inspect(value.response?.output);
      if (typeof value.response?.service_tier === "string") serviceTier = value.response.service_tier;
    } else if (value.type === "response.output_text.annotation.added") inspect(value.annotation);
  }
  return {calls: [...calls.values()], citations: [...citations].map(([url, title]) => ({url, title})),
    ...(serviceTier ? {serviceTier} : {})};
}

function failedStream(model: Model<Api>, interrupted: boolean, reason?: string) {
  const stream = createAssistantMessageEventStream();
  const message: AssistantMessage = {role: "assistant", content: [], api: model.api, provider: model.provider,
    model: model.id, timestamp: Date.now(), stopReason: interrupted ? "aborted" : "error",
    errorMessage: interrupted ? "Pi model request interrupted" : reason || "Pi model request failed",
    usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}}};
  stream.push({type: "error", reason: message.stopReason as "error" | "aborted", error: message});
  stream.end(message);
  return stream;
}

export function configureModel(session: AgentSession, settings: Obj, headers: Obj,
    prepare: () => Promise<{state: "work" | "final" | "denied"; agent_context: string[]; identity: Obj}>,
    agentDir: string, invoke: (payload: Obj) => Promise<Obj>,
    provider: {codex: boolean; chatgpt: boolean} = {codex: false, chatgpt: false},
    reportSearch?: (identity: Obj, attempt: number, evidence: SearchEvidence) => void,
    reportTier?: (identity: Obj, attempt: number, tier: string) => void) {
  const {codex, chatgpt} = provider;
  const subscription = codex || chatgpt;
  const nativeStream = session.agent.streamFunction;
  const capture = async (identity: Obj, attempt: number, phase: "request" | "response", value: unknown,
      boundary?: "openai_http_request") => {
    const captureId = randomUUID().replaceAll("-", "");
    let inspectedBytes = 0;
    const serialized = JSON.stringify(value, (key, item) => {
      inspectedBytes += Buffer.byteLength(key);
      if (typeof item === "string") inspectedBytes += Buffer.byteLength(item);
      if (inspectedBytes > MAX_MODEL_CAPTURE_BYTES)
        throw new Error("Pi Model trace exceeds the 32 MiB capture limit");
      return item;
    });
    if (serialized === undefined || Buffer.byteLength(serialized) > MAX_MODEL_CAPTURE_BYTES)
      throw new Error("Pi Model trace exceeds the 32 MiB capture limit");
    await writeFile(resolve(agentDir, `model-${captureId}.json`), serialized, {mode: 0o600, flag: "wx"});
    const receipt = await invoke({method: "model_trace", identity, attempt, phase, ...(boundary ? {boundary} : {}),
      capture_id: captureId, sha256: createHash("sha256").update(serialized).digest("hex")});
    if (receipt.method !== "model_trace" || !receipt.accepted || receipt.phase !== phase || receipt.attempt !== attempt)
      throw new Error("Invalid Pi Model trace acknowledgement");
  };
  session.agent.streamFunction = async (model, context, options) => {
    let requestCaptured = false;
    let responseAttempted = false;
    let identity: Obj | undefined;
    const attempt = session.retryAttempt;
    try {
      const permit = await prepare();
      identity = permit.identity;
      if (permit.state === "denied") return failedStream(model, false);
      const selectedContext = {...context,
        messages: permit.agent_context.length ? [...context.messages,
          {role: "user" as const, content: permit.agent_context.join("\n"), timestamp: Date.now()}] : context.messages};
      const events: unknown[] = [];
      const {reasoning_effort: _reasoning, web_search: _search, service_tier: _tier,
        extra_body: extraBody, ...samplingParams} = settings.extra_completion_params || {};
      const fetch = options?.fetch ?? globalThis.fetch;
      const stream = await nativeStream(model, selectedContext, {...options,
        temperature: subscription ? undefined : settings.temperature,
        maxTokens: settings.max_output_tokens, headers,
        cacheRetention: settings.context_cache ? "short" : "none",
        samplingParams: {...samplingParams, ...extraBody},
        fetch: async (input, init) => {
          const request = new Request(input, init);
          const baseUrl = codex ? "https://chatgpt.com/backend-api" : settings.base_url || "https://api.openai.com/v1";
          if (request.url.startsWith(baseUrl.replace(/\/$/, "") + "/") &&
              /\/(?:chat\/completions|responses)$/.test(new URL(request.url).pathname)) {
            let encoded = await readBounded(request.clone().body);
            if (request.headers.get("content-encoding") === "zstd") encoded = zlib.zstdDecompressSync(encoded);
            const safeHeaders = Object.fromEntries([...request.headers].filter(([name]) =>
              !["authorization", "cookie", "set-cookie", "chatgpt-account-id", "proxy-authorization"]
                .includes(name.toLowerCase())));
            await capture(permit.identity, attempt, "request", {method: request.method,
              url: request.url, headers: safeHeaders, body: JSON.parse(encoded.toString("utf8"))},
              "openai_http_request");
          }
          return fetch(input, init);
        },
        onPayload: async (payload, selectedModel) => {
          const outgoing = await options?.onPayload?.(payload, selectedModel) ?? payload;
          await capture(permit.identity, attempt, "request", outgoing);
          requestCaptured = true;
          return outgoing;
        },
        onProviderStreamEvent: async (data, selectedModel) => {
          if (subscription && ["response.output_item.done", "response.completed",
              "response.incomplete", "response.failed", "response.output_text.annotation.added"]
              .includes((data as Obj).type)) events.push(data);
          await options?.onProviderStreamEvent?.(data, selectedModel);
        },
      });
      const message = await stream.result();
      const evidence = subscription ? searchEvidence(events) : undefined;
      if (evidence) {
        if (settings.extra_completion_params?.web_search !== "off")
          reportSearch?.(permit.identity, attempt, evidence);
        if (evidence.serviceTier) reportTier?.(permit.identity, attempt, evidence.serviceTier);
      }
      if (requestCaptured) {
        responseAttempted = true;
        await capture(permit.identity, attempt, "response", evidence
          ? {...message, ...(settings.extra_completion_params?.web_search !== "off" ? {native_search: {
            calls: evidence.calls, citations: evidence.citations}} : {}),
            ...(evidence.serviceTier ? {native_service_tier: evidence.serviceTier} : {})}
          : message);
      }
      return stream;
    } catch (error) {
      if (requestCaptured && !responseAttempted && identity) {
        await capture(identity, attempt, "response", {
          stopReason: options?.signal?.aborted ? "aborted" : "error",
          errorMessage: error instanceof Error ? error.message : "Pi model transport failed",
        });
      }
      // SDK stream contract for an adapter/receipt failure, never a retry policy.
      return failedStream(model, options?.signal?.aborted ?? false);
    }
  };
}
