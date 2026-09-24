/** Profile policy around one public SDK model request; the SDK still owns its Agent loop. */
import { setTimeout as delay } from "node:timers/promises";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";
import { createAssistantMessageEventStream, type AssistantMessage, type Model, type Api } from "@earendil-works/pi-ai";
import type { AgentSession } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
let nextRequestAt = 0;

const EMPTY_IMAGE_PLACEHOLDER = "(see attached image)";
const httpCapture = new AsyncLocalStorage<(input: RequestInfo | URL, init?: RequestInit) => Promise<void>>();
const nativeFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = (input, init) => {
  const capture = httpCapture.getStore();
  return capture ? capture(input, init).then(() => nativeFetch(input, init)) : nativeFetch(input, init);
};

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
        message.content !== EMPTY_IMAGE_PLACEHOLDER) return message;
    changed = true;
    return {...message, content: ""};
  });
  if (Array.isArray(request.input)) result.input = request.input.map((item: Obj) => {
    if (item.type !== "function_call_output" || !callIds.has(item.call_id) ||
        item.output !== EMPTY_IMAGE_PLACEHOLDER) return item;
    changed = true;
    return {...item, output: ""};
  });
  return changed ? result : payload;
}

function failedStream(model: Model<Api>, interrupted: boolean) {
  const stream = createAssistantMessageEventStream();
  const message: AssistantMessage = {role: "assistant", content: [], api: model.api, provider: model.provider,
    model: model.id, timestamp: Date.now(), stopReason: interrupted ? "aborted" : "error",
    errorMessage: interrupted ? "Pi model request interrupted" : "Pi model request failed",
    usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}}};
  stream.push({type: "error", reason: message.stopReason as "error" | "aborted", error: message});
  stream.end(message);
  return stream;
}

export function configureModel(session: AgentSession, settings: Obj, headers: Obj,
    prepare: () => Promise<{state: "work" | "final" | "denied"; agent_context: string[]; identity: Obj}>,
    retry: (attempt: number) => void, agentDir: string, invoke: (payload: Obj) => Promise<Obj>) {
  const nativeStream = session.agent.streamFn;
  const failure = {timedOut: false, status: 0};
  const capture = async (identity: Obj, attempt: number, phase: "request" | "response", value: unknown,
      boundary?: "openai_http_request") => {
    const captureId = randomUUID().replaceAll("-", "");
    const bytes = Buffer.from(JSON.stringify(value));
    await writeFile(resolve(agentDir, `model-${captureId}.json`), bytes, {mode: 0o600, flag: "wx"});
    const receipt = await invoke({method: "model_trace", identity, attempt, phase, ...(boundary ? {boundary} : {}),
      capture_id: captureId, sha256: createHash("sha256").update(bytes).digest("hex")});
    if (receipt.method !== "model_trace" || !receipt.accepted || receipt.phase !== phase || receipt.attempt !== attempt)
      throw new Error("Invalid Pi Model trace acknowledgement");
  };
  session.agent.streamFn = async (model, context, options) => {
    try {
      const permit = await prepare();
      if (permit.state === "denied") return failedStream(model, false);
      const selectedContext = {...context, tools: permit.state === "final" ? [] : context.tools,
        messages: permit.agent_context.length ? [...context.messages,
          {role: "user" as const, content: permit.agent_context.join("\n"), timestamp: Date.now()}] : context.messages};
      for (let attempt = 0; ; attempt++) {
        failure.timedOut = false;
        failure.status = 0;
        const timeoutAbort = new AbortController();
        let timeout: ReturnType<typeof setTimeout> | undefined;
        const signal = options?.signal ? AbortSignal.any([options.signal, timeoutAbort.signal]) : timeoutAbort.signal;
        let stream;
        let errorText = "";
        const captureHttp = async (input: RequestInfo | URL, init?: RequestInit) => {
          const request = new Request(input, init);
          const baseUrl = settings.base_url || "https://api.openai.com/v1";
          if (!request.url.startsWith(baseUrl.replace(/\/$/, "") + "/") ||
              !/\/(?:chat\/completions|responses)$/.test(new URL(request.url).pathname)) return;
          const body = JSON.parse(await request.clone().text());
          await capture(permit.identity, attempt, "request",
            {method: request.method, url: request.url, headers: Object.fromEntries(request.headers), body},
            "openai_http_request");
        };
        try {
          stream = await httpCapture.run(captureHttp, () => nativeStream(model, selectedContext, {...options, signal,
            temperature: settings.temperature, maxTokens: settings.max_output_tokens, headers,
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
              return outgoing;
            },
            onResponse: async (response, selectedModel) => {
              failure.status = response.status;
              await options?.onResponse?.(response, selectedModel);
            }}));
          // Pi's public stream.result() resolves without iteration. Hold this one
          // response until retry selection finishes; never rewind Agent messages or tools.
          const message = await stream.result();
          await capture(permit.identity, attempt, "response", message);
          if (!failure.timedOut && message.stopReason !== "error") return stream;
          errorText = message.errorMessage || "";
        } catch (error) {
          errorText = error instanceof Error ? error.message : "";
        } finally {
          if (timeout !== undefined) clearTimeout(timeout);
        }
        const errorStatus = /^(\d{3})\b/.exec(errorText);
        if (errorStatus) failure.status = Number(errorStatus[1]);
        const retryable = failure.timedOut || failure.status === 408 || failure.status === 429 || failure.status >= 500 ||
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
