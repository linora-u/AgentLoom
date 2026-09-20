/** One SDK session per managed process. stdout is exclusively protocol JSONL. */
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { decode } from "./protocol.js";
import {
  AuthStorage, ModelRegistry, SettingsManager, SessionManager, DefaultResourceLoader,
  createAgentSession, type AgentSession,
} from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
type Frame = {version: 1; kind: string; instance_id: string; run_id: string | null; request_id: string; payload: Obj};
const sdkPackage = new URL("../package.json", import.meta.resolve("@earendil-works/pi-coding-agent"));
const sdkVersion = JSON.parse(readFileSync(sdkPackage, "utf8")).version as string;
const capabilities = {structured_tools: false, parallel_tools: false, checkpoint_resume: false, subagents: false, goal: false, stop_hooks: true};
const agentDir = process.argv[2];
let instance: string | undefined;
let session: AgentSession | undefined;
let current: {frame: Frame; abort: AbortController} | undefined;
let closing = false;
let nextRequestAt = 0;
const seen = new Set<string>();
const write = (value: unknown) => process.stdout.write(JSON.stringify(value) + "\n");
const response = (frame: Frame, payload: Obj) => write({version: 1, kind: "response", instance_id: frame.instance_id, run_id: frame.run_id, request_id: frame.request_id, payload, error: null});
const failure = (frame: Frame, category: string, message: string) => write({version: 1, kind: "response", instance_id: frame.instance_id, run_id: frame.run_id, request_id: frame.request_id, payload: null, error: {category, message, retryable: false}});

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
  const loader = new DefaultResourceLoader({cwd: p.cwd, agentDir, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPrompt: p.instructions});
  await loader.reload();
  const {session: created} = await createAgentSession({cwd: p.cwd, agentDir, authStorage: auth, modelRegistry: registry,
    model: registry.find("agentloom", modelId), thinkingLevel: "off", tools: [], noTools: "all", customTools: [],
    resourceLoader: loader, settingsManager: settings, sessionManager: SessionManager.inMemory(p.cwd)});
  const nativeStream = created.agent.streamFn;
  created.agent.streamFn = async (model, context, options) => {
    await delay(Math.max(0, nextRequestAt - performance.now()), undefined, {signal: options?.signal});
    nextRequestAt = performance.now() + 60000 / s.requests_per_minute;
    return nativeStream(model, context, {...options,
      temperature: s.temperature, maxTokens: s.max_output_tokens, headers: p.model.request_headers,
      cacheRetention: s.context_cache ? "short" : "none", transport: "sse"});
  };
  created.agent.onPayload = (payload) => {
    const result = {...payload as Obj};
    const extra = s.extra_completion_params || {};
    for (const key of ["top_p", "seed"]) if (extra[key] !== undefined) result[key] = extra[key];
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
  let unsubscribe: (() => void) | undefined;
  try {
    if (p.tools.length || p.checkpoint || Object.keys(p.additional_args).length)
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
      // Public AgentSession owns the complete provider call and turn lifecycle.
      await session.prompt(p.task, {expandPromptTemplates: false});
      last = session.messages.at(-1);
      if (last?.role !== "assistant" || last.stopReason !== "error") break;
      // Pi preserves SDK APIError status at the start of its private errorMessage.
      // Never publish the provider text (it can echo credentials or prompts).
      const errorText = String(last.errorMessage || "");
      const errorStatus = /^(\d{3})\b/.exec(errorText);
      if (errorStatus) status = Number(errorStatus[1]);
      const retryable = status === 408 || status === 429 || status >= 500 ||
        (!errorStatus && /connection|network|fetch failed|timed? out|timeout|stream ended/i.test(errorText));
      if (!retryable || attempt >= settings.num_retries) break;
      event("model", {phase: "retry", attempt: attempt + 1});
      await delay(Math.min(settings.max_retry_delay, settings.retry_delay * 2 ** Math.min(attempt, 30)) * 1000,
        undefined, {signal: abort.signal});
    }
    if (abort.signal.aborted || last?.stopReason === "aborted") throw new Error("Interrupted");
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
      error: {category: interrupted ? "interrupted" : "provider", message: interrupted ? "Pi run interrupted" : "Pi model request failed", retryable: false}});
  } finally {
    unsubscribe?.();
    current = undefined;
  }
}

async function accept(frame: Frame) {
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
    void run(frame, current.abort);
  } else if (p.method === "cancel") {
    const target = current?.frame;
    const accepted = target?.request_id === p.target_request_id && target?.run_id === frame.run_id;
    if (accepted) {current!.abort.abort(); void session?.abort();}
    response(frame, {method: "cancel", accepted});
  } else if (p.method === "snapshot") {
    response(frame, {method: "snapshot", checkpoint: null});
  } else if (p.method === "close") {
    closing = true;
    current?.abort.abort();
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
