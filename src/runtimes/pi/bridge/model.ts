/** Profile policy around one public SDK model request; the SDK still owns its Agent loop. */
import { setTimeout as delay } from "node:timers/promises";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";
import * as zlib from "node:zlib";
import { createAssistantMessageEventStream, type AssistantMessage, type Model, type Api } from "@earendil-works/pi-ai";
import type { AgentSession } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
let nextRequestAt = 0;

const EMPTY_TOOL_PLACEHOLDERS = new Set(["(see attached image)", "(no tool output)"]);
const MAX_MODEL_CAPTURE_BYTES = 32 * 1024 * 1024;
export type SearchEvidence = {calls: {id: string; status: "completed"}[];
  citations: {url: string; title: string}[]};
const httpCapture = new AsyncLocalStorage<(input: RequestInfo | URL, init: RequestInit | undefined,
  send: () => Promise<Response>) => Promise<Response>>();
const nativeFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = (input, init) => {
  const capture = httpCapture.getStore();
  return capture ? capture(input, init, () => nativeFetch(input, init)) : nativeFetch(input, init);
};

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

function searchEvidence(raw: string): SearchEvidence {
  const calls = new Map<string, {id: string; status: "completed"}>();
  const citations = new Map<string, string>();
  const inspect = (value: unknown): void => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) {for (const part of value) inspect(part); return;}
    const item = value as Obj;
    if (item.type === "web_search_call" && item.status === "completed" && calls.size < 50) {
      const id = typeof item.id === "string" ? item.id.slice(0, 150) : `search-${calls.size + 1}`;
      calls.set(id, {id, status: "completed"});
    }
    if (item.type === "url_citation" && typeof item.url === "string" && item.url.length <= 2048 && citations.size < 100) {
      try {
        const url = new URL(item.url);
        if (url.protocol === "https:" || url.protocol === "http:")
          citations.set(url.href, typeof item.title === "string" && item.title.trim()
            ? item.title.trim().replace(/[\r\n]/g, " ").slice(0, 250) : url.hostname);
      } catch { /* Ignore invalid provider citations. */ }
    }
    for (const part of Object.values(item)) inspect(part);
  };
  for (const frame of raw.replaceAll("\r\n", "\n").split("\n\n")) {
    const data = frame.split("\n").filter(line => line.startsWith("data:"))
      .map(line => line.slice(5).trimStart()).join("\n");
    if (!data || data === "[DONE]") continue;
    try {
      const event = JSON.parse(data) as Obj;
      if (event.type === "response.output_item.done") inspect(event.item);
      else if (event.type === "response.completed") inspect(event.response?.output);
      else if (event.type === "response.output_text.annotation.added") inspect(event.annotation);
    } catch { /* Ignore non-JSON SSE heartbeat data. */ }
  }
  return {calls: [...calls.values()], citations: [...citations].map(([url, title]) => ({url, title}))};
}

function emptyToolCallIds(messages: readonly Obj[]) {
  const callIds = new Set<string>();
  for (const message of messages) {
    if (message.role !== "toolResult" || !Array.isArray(message.content)) continue;
    const parts = message.content as Obj[];
    if (!parts.length || parts.some(part => part.type !== "text" || typeof part.text !== "string") ||
        parts.map(part => part.text).join("\n") !== "") continue;
    const callId = message.toolCallId;
    if (typeof callId !== "string") continue;
    callIds.add(callId);
    callIds.add(callId.split("|", 1)[0]);
  }
  return callIds;
}

function restoreEmptyToolResults(payload: unknown, messages: readonly Obj[]) {
  if (!payload || typeof payload !== "object") return payload;
  const callIds = emptyToolCallIds(messages);
  if (!callIds.size) return payload;
  const request = payload as Obj;
  let changed = false;
  const result = {...request};
  if (Array.isArray(request.messages)) result.messages = request.messages.map((message: Obj) => {
    if (message.role !== "tool" || !callIds.has(message.tool_call_id) ||
        !EMPTY_TOOL_PLACEHOLDERS.has(message.content)) return message;
    changed = true;
    return {...message, content: ""};
  });
  if (Array.isArray(request.input)) result.input = request.input.map((item: Obj) => {
    if (item.type !== "function_call_output" || !callIds.has(item.call_id) ||
        !EMPTY_TOOL_PLACEHOLDERS.has(item.output)) return item;
    changed = true;
    return {...item, output: ""};
  });
  return changed ? result : payload;
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
    retry: (attempt: number) => void, agentDir: string, invoke: (payload: Obj) => Promise<Obj>,
    codex = false, reportSearch?: (identity: Obj, attempt: number, evidence: SearchEvidence) => void) {
  const nativeStream = session.agent.streamFunction;
  const failure = {timedOut: false, status: 0, reason: ""};
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
    try {
      const permit = await prepare();
      if (permit.state === "denied") return failedStream(model, false);
      const selectedContext = {...context, tools: permit.state === "final" ? [] : (context as Obj).tools,
        messages: permit.agent_context.length ? [...context.messages,
          {role: "user" as const, content: permit.agent_context.join("\n"), timestamp: Date.now()}] : context.messages};
      for (let attempt = 0; ; attempt++) {
        failure.timedOut = false;
        failure.status = 0;
        failure.reason = "";
        const timeoutAbort = new AbortController();
        let timeout: ReturnType<typeof setTimeout> | undefined;
        const signal = options?.signal ? AbortSignal.any([options.signal, timeoutAbort.signal]) : timeoutAbort.signal;
        let stream;
        let errorText = "";
        let requestCaptured = false;
        let responseAttempted = false;
        let searchPromise: Promise<SearchEvidence> | undefined;
        const captureHttp = async (input: RequestInfo | URL, init: RequestInit | undefined,
            send: () => Promise<Response>): Promise<Response> => {
          const request = new Request(input, init);
          const baseUrl = codex ? "https://chatgpt.com/backend-api" : settings.base_url || "https://api.openai.com/v1";
          if (!request.url.startsWith(baseUrl.replace(/\/$/, "") + "/") ||
              !/\/(?:chat\/completions|responses)$/.test(new URL(request.url).pathname)) return send();
          let encoded = await readBounded(request.clone().body);
          if (request.headers.get("content-encoding") === "zstd") {
            const decompress = (zlib as unknown as {zstdDecompressSync?: (bytes: Buffer) => Buffer}).zstdDecompressSync;
            if (!decompress) throw new Error("Node cannot inspect Pi's zstd Codex request");
            encoded = decompress(encoded);
          }
          if (encoded.byteLength > MAX_MODEL_CAPTURE_BYTES)
            throw new Error("Pi Model decoded HTTP request exceeds the 32 MiB limit");
          const body = JSON.parse(encoded.toString("utf8"));
          const safeHeaders = Object.fromEntries([...request.headers].filter(([name]) =>
            !["authorization", "cookie", "set-cookie", "chatgpt-account-id", "proxy-authorization"]
              .includes(name.toLowerCase())));
          await capture(permit.identity, attempt, "request",
            {method: request.method, url: request.url, headers: safeHeaders, body},
            "openai_http_request");
          const response = await send();
          if (codex && settings.extra_completion_params?.web_search !== "off" && response.ok) {
            searchPromise = readBounded(response.clone().body).then(bytes => searchEvidence(bytes.toString("utf8")));
            void searchPromise.catch(() => {});
          }
          return response;
        };
        try {
          stream = await httpCapture.run(captureHttp, () => nativeStream(model, selectedContext, {...options, signal,
            temperature: codex ? undefined : settings.temperature, maxTokens: settings.max_output_tokens, headers,
            cacheRetention: settings.context_cache ? "short" : "none", transport: "sse",
            onPayload: async (payload, selectedModel) => {
              const projected = await options?.onPayload?.(payload, selectedModel);
              await delay(Math.max(0, nextRequestAt - performance.now()), undefined, {signal: options?.signal});
              nextRequestAt = performance.now() + 60000 / settings.requests_per_minute;
              timeout = setTimeout(() => {failure.timedOut = true; timeoutAbort.abort();}, settings.timeout * 1000);
              const outgoing = restoreEmptyToolResults(
                projected === undefined ? payload : projected,
                selectedContext.messages as Obj[],
              );
              await capture(permit.identity, attempt, "request", outgoing);
              requestCaptured = true;
              return outgoing;
            },
            onResponse: async (response, selectedModel) => {
              failure.status = response.status;
              await options?.onResponse?.(response, selectedModel);
            }}));
          // Pi's public stream.result() resolves without iteration. Hold this one
          // response until retry selection finishes; never rewind Agent messages or tools.
          const message = await stream.result();
          let evidence: SearchEvidence | undefined;
          if (searchPromise) {
            evidence = await searchPromise;
            reportSearch?.(permit.identity, attempt, evidence);
          }
          if (codex && settings.extra_completion_params?.web_search === "required" &&
              message.stopReason !== "error" && message.stopReason !== "aborted" &&
              !evidence?.calls.length)
            throw new Error("Pi Codex required web search was not executed");
          // An already-aborted turn can produce an SDK error stream without
          // reaching onPayload. There is no provider request to pair it with.
          if (requestCaptured) {
            responseAttempted = true;
            await capture(permit.identity, attempt, "response", evidence ? {...message, native_search: evidence} : message);
          }
          if (!failure.timedOut && message.stopReason !== "error") return stream;
          errorText = message.errorMessage || "";
        } catch (error) {
          // A response capture failure cannot turn a successful provider
          // stream into an unrecorded successful Agent turn.
          if (responseAttempted) throw error;
          errorText = error instanceof Error ? error.message : "";
          if (requestCaptured && !responseAttempted) {
            responseAttempted = true;
            await capture(permit.identity, attempt, "response", {
              stopReason: options?.signal?.aborted ? "aborted" : "error",
              errorMessage: errorText || "Pi model transport failed",
            });
          }
          stream = failedStream(model, options?.signal?.aborted ?? false, errorText);
        } finally {
          if (timeout !== undefined) clearTimeout(timeout);
        }
        const errorStatus = /^(\d{3})\b/.exec(errorText);
        if (errorStatus) failure.status = Number(errorStatus[1]);
        const quotaExhausted = /usage limit|insufficient_quota|out of budget|quota exceeded|billing/i.test(errorText);
        failure.reason = codex ?
          errorText === "Pi Codex required web search was not executed" ? errorText :
          failure.status === 401 || failure.status === 403 ? "Pi Codex authentication failed; log in through Pi again" :
          failure.status === 404 ? "Pi Codex model is unavailable" :
          quotaExhausted ? "Pi Codex subscription quota exhausted" :
          "Pi Codex provider request failed" : "Pi model request failed";
        const retryable = failure.timedOut || failure.status === 408 || (failure.status === 429 && !quotaExhausted) || failure.status >= 500 ||
          (!errorStatus && /connection|network|fetch failed|timed? out|timeout|stream ended/i.test(errorText));
        if (options?.signal?.aborted || !retryable || attempt >= settings.num_retries)
          return stream || failedStream(model, options?.signal?.aborted ?? false);
        retry(attempt + 1);
        await delay(Math.min(settings.max_retry_delay, settings.retry_delay * 2 ** Math.min(attempt, 30)) * 1000,
          undefined, {signal: options?.signal});
      }
    } catch {
      // StreamFn failures must use the public SDK error stream contract.
      return failedStream(model, options?.signal?.aborted ?? false);
    }
  };
  return failure;
}
