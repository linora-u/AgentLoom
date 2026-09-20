/** One SDK session per managed process. stdout is exclusively protocol JSONL. */
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { decode } from "./protocol.js";
import { nativeTools } from "./tools.js";
import { randomUUID } from "node:crypto";
import {
  AuthStorage, ModelRegistry, SettingsManager, SessionManager, DefaultResourceLoader,
  createAgentSession, type AgentSession,
} from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
type Frame = {version: 1; kind: string; instance_id: string; run_id: string | null; request_id: string; payload: Obj};
const sdkPackage = new URL("../package.json", import.meta.resolve("@earendil-works/pi-coding-agent"));
const sdkVersion = JSON.parse(readFileSync(sdkPackage, "utf8")).version as string;
const capabilities = {structured_tools: true, parallel_tools: true, checkpoint_resume: false, subagents: true, goal: true, stop_hooks: true};
const agentDir = process.argv[2];
let instance: string | undefined;
let session: AgentSession | undefined;
let current: {frame: Frame; abort: AbortController} | undefined;
let closing = false;
let nextRequestAt = 0;
const seen = new Set<string>();
const callbacks = new Map<string, {runId: string | null; method: string; resolve: (value: Obj) => void; reject: (error: Error) => void}>();
let calledTool = false;
const write = (value: unknown) => process.stdout.write(JSON.stringify(value) + "\n");
const response = (frame: Frame, payload: Obj) => write({version: 1, kind: "response", instance_id: frame.instance_id, run_id: frame.run_id, request_id: frame.request_id, payload, error: null});
const failure = (frame: Frame, category: string, message: string) => write({version: 1, kind: "response", instance_id: frame.instance_id, run_id: frame.run_id, request_id: frame.request_id, payload: null, error: {category, message, retryable: false}});

function invoke(payload: Obj): Promise<Obj> {
  const active = current;
  if (!active || active.abort.signal.aborted) return Promise.reject(new Error("Inactive Pi run"));
  calledTool = true;
  const requestId = `pi:${randomUUID()}`;
  return new Promise((resolve, reject) => {
    callbacks.set(requestId, {runId: active.frame.run_id, method: payload.method, resolve, reject});
    write({version: 1, kind: "request", instance_id: instance, run_id: active.frame.run_id, request_id: requestId, payload});
  });
}

function rejectCallbacks() {
  for (const pending of callbacks.values()) pending.reject(new Error("Pi run interrupted"));
  callbacks.clear();
}

async function createSession(p: Obj): Promise<AgentSession> {
  const s = p.model.settings;
  const auth = AuthStorage.inMemory();
  auth.setRuntimeApiKey("agentloom", s.api_key || "no-key");
  const registry = ModelRegistry.inMemory(auth);
  const api = p.model.protocol === "openai_chat" ? "openai-completions" : "openai-responses";
  const modelId = p.model.model_id.replace(/^(openai|gemini)\//, "");
  registry.registerProvider("agentloom", {
    api, baseUrl: s.base_url || "https://api.openai.com/v1", apiKey: "agentloom-runtime-key",
    models: [{id: modelId, name: modelId, reasoning: false, input: ["text"],
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}, contextWindow: s.context_window,
      maxTokens: s.max_output_tokens, compat: {supportsDeveloperRole: false, supportsUsageInStreaming: true, maxTokensField: "max_tokens"}}],
  });
  // Adapter-controlled, bounded retry waits; no hidden SDK retry multiplier.
  const settings = SettingsManager.inMemory({retry: {enabled: false, provider: {maxRetries: 0, timeoutMs: s.timeout * 1000}},
    compaction: {enabled: false}, enableAnalytics: false, enableInstallTelemetry: false, packages: []});
  const manager = SessionManager.inMemory(p.cwd);
  const selected = nativeTools(p.tools, p.cwd, invoke, callId => ({application_id: current!.frame.payload.application_id,
    task_id: current!.frame.payload.task_id, run_id: current!.frame.run_id, instance_id: instance, call_id: callId,
    native_session_id: manager.getSessionId(), native_parent_id: manager.getLeafId()}));
  const loader = new DefaultResourceLoader({cwd: p.cwd, agentDir, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPrompt: p.instructions, extensionFactories: [selected.extension]});
  await loader.reload();
  const {session: created} = await createAgentSession({cwd: p.cwd, agentDir, authStorage: auth, modelRegistry: registry,
    model: registry.find("agentloom", modelId), thinkingLevel: "off", tools: selected.tools.map(tool => tool.name),
    noTools: "all", customTools: selected.tools,
    resourceLoader: loader, settingsManager: settings, sessionManager: manager});
  await created.bindExtensions({onError: () => created.agent.abort()});
  const nativeStream = created.agent.streamFn;
  created.agent.streamFn = (model, context, options) => {
    return nativeStream(model, context, {...options,
      temperature: s.temperature, maxTokens: s.max_output_tokens, headers: p.model.request_headers,
      cacheRetention: s.context_cache ? "short" : "none", transport: "sse"});
  };
  created.agent.onPayload = (payload) => {
    const result = {...payload as Obj};
    const extra = s.extra_completion_params || {};
    for (const key of ["top_p", "seed"]) if (extra[key] !== undefined) result[key] = extra[key];
    if (p.tools.length) for (const key of ["tool_choice", "parallel_tool_calls"])
      if (extra[key] !== undefined) result[key] = extra[key];
    if (extra.reasoning_effort !== undefined) {
      if (api === "openai-responses") result.reasoning = {effort: extra.reasoning_effort};
      else result.reasoning_effort = extra.reasoning_effort;
    }
    return {...result, ...extra.extra_body};
  };
  return created;
}

async function run(frame: Frame, abort: AbortController) {
  const p = frame.payload;
  let seq = 0;
  const event = (kind: string, payload: Obj) => write({version: 1, kind: "event", instance_id: frame.instance_id,
    run_id: frame.run_id, request_id: frame.request_id, sequence: ++seq, event: kind, payload});
  const usage = {input_tokens: 0, output_tokens: 0, total_tokens: 0, cached_input_tokens: 0};
  let unavailableTool = false;
  let timedOut = false;
  let unsubscribe: (() => void) | undefined;
  try {
    if (p.checkpoint)
      throw new Error("Unsupported request");
    if (!p.continue_session || !session) {
      session?.dispose();
      session = await createSession(p);
    }
    if (abort.signal.aborted) throw new Error("Interrupted");
    event("run", {phase: "started", resumed: false});
    unsubscribe = session.subscribe(e => {
      if (e.type === "message_start" && e.message.role === "assistant") event("model", {phase: "started"});
      if (e.type === "message_end" && e.message.role === "assistant") {
        if (e.message.content.some(block => block.type === "toolCall" && !p.tools.some((tool: Obj) => tool.visible_name === block.name))) {
          unavailableTool = true;
          session!.agent.abort();
        }
        const u = e.message.usage;
        usage.input_tokens += u.input + u.cacheRead + u.cacheWrite;
        usage.output_tokens += u.output;
        usage.total_tokens += u.totalTokens;
        usage.cached_input_tokens += u.cacheRead;
        event("model", {phase: "completed", stop_reason: e.message.stopReason});
      }
    });
    const before = [...session.agent.state.messages];
    let last: any;
    let status = 0;
    session.agent.onResponse = response => {status = response.status;};
    const settings = p.model.settings;
    for (let attempt = 0; ; attempt++) {
      if (abort.signal.aborted) throw new Error("Interrupted");
      if (attempt) session.agent.state.messages = before;
      await delay(Math.max(0, nextRequestAt - performance.now()), undefined, {signal: abort.signal});
      nextRequestAt = performance.now() + 60000 / settings.requests_per_minute;
      timedOut = false;
      status = 0;
      const timeout = setTimeout(() => {timedOut = true; session!.agent.abort();}, settings.timeout * 1000);
      // Public AgentSession owns the complete provider call and turn lifecycle.
      const task = Object.keys(p.additional_args).length ? `${p.task}\n\nAgentLoom task inputs (JSON):\n${JSON.stringify(p.additional_args)}` : p.task;
      try {await session.prompt(task, {expandPromptTemplates: false});}
      finally {clearTimeout(timeout);}
      last = session.messages.at(-1);
      if (!timedOut && (last?.role !== "assistant" || last.stopReason !== "error")) break;
      // Pi preserves SDK APIError status at the start of its private errorMessage.
      // Never publish the provider text (it can echo credentials or prompts).
      const errorText = String(last?.errorMessage || "");
      const errorStatus = /^(\d{3})\b/.exec(errorText);
      if (errorStatus) status = Number(errorStatus[1]);
      const retryable = timedOut || status === 408 || status === 429 || status >= 500 ||
        (!errorStatus && /connection|network|fetch failed|timed? out|timeout|stream ended/i.test(errorText));
      if (!retryable || calledTool || attempt >= settings.num_retries) break;
      event("model", {phase: "retry", attempt: attempt + 1});
      await delay(Math.min(settings.max_retry_delay, settings.retry_delay * 2 ** Math.min(attempt, 30)) * 1000,
        undefined, {signal: abort.signal});
    }
    if (timedOut || unavailableTool || abort.signal.aborted || last?.stopReason === "aborted") throw new Error("Interrupted");
    const state = last?.role !== "assistant" || last.stopReason === "error" ? "failed" :
      last.stopReason === "length" ? "max_steps_error" : "success";
    const output = last?.content?.filter((b: Obj) => b.type === "text").map((b: Obj) => b.text).join("") ?? "";
    event("usage", usage);
    // Host emits the public terminal event only after its Stop gate.
    response(frame, {method: "run", state, output, usage, artifacts: [], checkpoint: null,
      error: state === "failed" ? {category: "provider", message: "Pi model request failed", retryable: status === 429 || status >= 500} : null});
  } catch {
    const interrupted = abort.signal.aborted;
    response(frame, {method: "run", state: interrupted ? "interrupted" : "failed", output: null, usage, artifacts: [], checkpoint: null,
      error: {category: interrupted ? "interrupted" : "provider", message: unavailableTool ? "Pi model requested an unavailable tool" : interrupted ? "Pi run interrupted" : timedOut ? "Pi model request timed out" : "Pi model request failed", retryable: timedOut}});
  } finally {
    rejectCallbacks();
    unsubscribe?.();
    current = undefined;
  }
}

async function accept(frame: Frame) {
  if (frame.kind === "response") {
    const pending = callbacks.get(frame.request_id);
    if (!pending || frame.instance_id !== instance || frame.run_id !== pending.runId ||
        (frame.payload && frame.payload.method !== pending.method)) throw new Error();
    callbacks.delete(frame.request_id);
    if ((frame as any).error) pending.reject(new Error("AgentLoom callback failed"));
    else pending.resolve(frame.payload);
    return;
  }
  if (frame.version !== 1 || frame.kind !== "request" || !frame.request_id?.startsWith("host:") ||
      !frame.instance_id || (instance && instance !== frame.instance_id) || seen.has(frame.request_id)) throw new Error();
  if (closing) throw new Error();
  seen.add(frame.request_id);
  const p = frame.payload;
  if (p.method === "handshake") {
    if (instance || p.native_tool_contract !== 1) throw new Error();
    instance = frame.instance_id;
    const [major, minor] = process.versions.node.split(".").map(Number);
    if (major < 22 || (major === 22 && minor < 19)) throw new Error();
    response(frame, {method: "handshake", runtime_id: "pi", sdk_version: sdkVersion,
      node_version: process.versions.node, native_tool_contract: 1, capabilities});
    return;
  }
  if (!instance) throw new Error();
  if (p.method === "run") {
    if (current || !frame.run_id || !Array.isArray(p.tools)) throw new Error();
    current = {frame, abort: new AbortController()};
    calledTool = false;
    void run(frame, current.abort);
  } else if (p.method === "cancel") {
    const target = current?.frame;
    const accepted = target?.request_id === p.target_request_id && target?.run_id === frame.run_id;
    if (accepted) {current!.abort.abort(); rejectCallbacks(); void session?.abort();}
    response(frame, {method: "cancel", accepted});
  } else if (p.method === "snapshot") {
    response(frame, {method: "snapshot", checkpoint: null});
  } else if (p.method === "close") {
    closing = true;
    current?.abort.abort();
    rejectCallbacks();
    await session?.abort();
    session?.dispose();
    response(frame, {method: "close", accepted: true});
    process.stdout.write("", () => process.exit(0));
  } else failure(frame, "unsupported_capability", "Unsupported Pi method");
}

const input = createInterface({input: process.stdin, crlfDelay: Infinity});
input.on("line", line => {
  try {
    if (Buffer.byteLength(line) > 8 * 1024 * 1024) throw new Error();
    void accept(decode(line) as Frame).catch(() => process.exit(2));
  } catch {process.exit(2);}
});
input.on("close", () => {current?.abort.abort(); void session?.abort().finally(() => process.exit(0)); if (!session) process.exit(0);});
