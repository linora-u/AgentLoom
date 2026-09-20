/** Profile policy around one public SDK model request; the SDK still owns its Agent loop. */
import { setTimeout as delay } from "node:timers/promises";
import { createAssistantMessageEventStream, type AssistantMessage, type Model, type Api } from "@earendil-works/pi-ai";
import type { AgentSession } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
let nextRequestAt = 0;

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
    prepare: () => Promise<"work" | "final" | "denied">, retry: (attempt: number) => void) {
  const nativeStream = session.agent.streamFn;
  const failure = {timedOut: false, status: 0};
  session.agent.streamFn = async (model, context, options) => {
    try {
      const permit = await prepare();
      if (permit === "denied") return failedStream(model, false);
      const selectedContext = permit === "final" ? {...context, tools: []} : context;
      for (let attempt = 0; ; attempt++) {
        await delay(Math.max(0, nextRequestAt - performance.now()), undefined, {signal: options?.signal});
        nextRequestAt = performance.now() + 60000 / settings.requests_per_minute;
        failure.timedOut = false;
        failure.status = 0;
        const timeoutAbort = new AbortController();
        const timeout = setTimeout(() => {failure.timedOut = true; timeoutAbort.abort();}, settings.timeout * 1000);
        const signal = options?.signal ? AbortSignal.any([options.signal, timeoutAbort.signal]) : timeoutAbort.signal;
        let stream;
        let errorText = "";
        try {
          stream = await nativeStream(model, selectedContext, {...options, signal,
            temperature: settings.temperature, maxTokens: settings.max_output_tokens, headers,
            cacheRetention: settings.context_cache ? "short" : "none", transport: "sse",
            onResponse: async (response, selectedModel) => {
              failure.status = response.status;
              await options?.onResponse?.(response, selectedModel);
            }});
          // Pi's public stream.result() resolves without iteration. Hold this one
          // response until retry selection finishes; never rewind Agent messages or tools.
          const message = await stream.result();
          if (!failure.timedOut && message.stopReason !== "error") return stream;
          errorText = message.errorMessage || "";
        } catch (error) {
          errorText = error instanceof Error ? error.message : "";
        } finally {
          clearTimeout(timeout);
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
