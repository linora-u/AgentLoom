/** One SDK session per managed process. stdout is exclusively protocol JSONL. */
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { isDeepStrictEqual } from "node:util";
import { Ajv2020 } from "ajv/dist/2020.js";
import type { ValidateFunction } from "ajv";
import { configureModel, type SearchEvidence } from "./model.js";
import { decode } from "./protocol.js";
import { nativeTools } from "./tools.js";
import { restoreSession, SessionPersistence } from "./checkpoint.js";
import { enableInstructionOnlyTurns, runInstructionOnlyTurn } from "./session.js";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import {
  ModelRuntime, SettingsManager, SessionManager, DefaultResourceLoader,
  createAgentSession, type AgentSession,
} from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
type Frame = {version: 2; kind: string; instance_id: string; run_id: string | null; request_id: string; payload: Obj};
const sdkPackage = new URL("../package.json", import.meta.resolve("@earendil-works/pi-coding-agent"));
const sdkVersion = JSON.parse(readFileSync(sdkPackage, "utf8")).version as string;
const capabilities = {structured_tools: true, parallel_tools: true, checkpoint_resume: true, subagents: true, goal: true, stop_hooks: true, structured_output: true};
const agentDir = process.argv[2];
const piAuthPath = process.argv[3];
let instance: string | undefined;
let session: AgentSession | undefined;
let persistence: SessionPersistence | undefined;
let restoredPhase: string | undefined;
let current: {frame: Frame; abort: AbortController} | undefined;
let closing = false;
let nativeIncomplete = false;
let modelFailure = {timedOut: false, status: 0, reason: ""};
let reportRetry: ((attempt: number) => void) | undefined;
let reportSearch: ((identity: Obj, attempt: number, result: SearchEvidence) => void) | undefined;
let outputCorrection = false;
const seen = new Set<string>();
const callbacks = new Map<string, {runId: string | null; method: string; resolve: (value: Obj) => void; reject: (error: Error) => void}>();
const write = (value: unknown) => process.stdout.write(JSON.stringify(value) + "\n");
const response = (frame: Frame, payload: Obj) => write({version: 2, kind: "response", instance_id: frame.instance_id, run_id: frame.run_id, request_id: frame.request_id, payload, error: null});
const failure = (frame: Frame, category: string, message: string) => write({version: 2, kind: "response", instance_id: frame.instance_id, run_id: frame.run_id, request_id: frame.request_id, payload: null, error: {category, message, retryable: false}});

function textualToolCallName(text: string): string | null {
  if (!/<[｜|]DSML[｜|]tool_calls>/.test(text)) return null;
  return /<[｜|]DSML[｜|]invoke\s+name="([A-Za-z_][\w.-]{0,79})"/.exec(text)?.[1] ?? null;
}

function invoke(payload: Obj): Promise<Obj> {
  const active = current;
  if (!active || active.abort.signal.aborted) return Promise.reject(new Error("Inactive Pi run"));
  const requestId = `pi:${randomUUID()}`;
  return new Promise((resolve, reject) => {
    callbacks.set(requestId, {runId: active.frame.run_id, method: payload.method, resolve, reject});
    write({version: 2, kind: "request", instance_id: instance, run_id: active.frame.run_id, request_id: requestId, payload});
  });
}

function rejectCallbacks() {
  for (const pending of callbacks.values()) pending.reject(new Error("Pi run interrupted"));
  callbacks.clear();
}

async function createSession(p: Obj): Promise<AgentSession> {
  nativeIncomplete = false;
  outputCorrection = false;
  const s = p.model.settings;
  const codex = p.model.protocol === "openai_codex_responses";
  if (codex && !piAuthPath) throw new Error("Pi Codex credential path is unavailable");
  const runtime = await ModelRuntime.create({
    authPath: codex ? piAuthPath : join(agentDir, "auth.json"),
    modelsPath: null, refreshOnCreate: false,
  });
  const api = codex ? "openai-codex-responses" :
    p.model.protocol === "openai_chat" ? "openai-completions" : "openai-responses";
  const modelId = codex ? p.model.model_id : p.model.model_id.replace(/^(openai|gemini)\//, "");
  if (!codex) {
    runtime.registerProvider("agentloom", {
      api, baseUrl: s.base_url || "https://api.openai.com/v1", apiKey: "agentloom-runtime-key",
      models: [{id: modelId, name: modelId, reasoning: false, input: ["text"],
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}, contextWindow: s.context_window,
        maxTokens: s.max_output_tokens, compat: {supportsDeveloperRole: false, supportsUsageInStreaming: true, maxTokensField: "max_tokens"}}],
    });
    await runtime.setRuntimeApiKey("agentloom", s.api_key || "no-key");
  }
  const model = runtime.getModel(codex ? "openai-codex" : "agentloom", modelId);
  if (!model) throw new Error(codex ? `Pi Codex model ${modelId} is unavailable` : `Pi model ${modelId} is unavailable`);
  if (codex) {
    let auth;
    try {auth = await runtime.getAuth(model, {signal: current?.abort.signal});}
    catch {throw new Error("Pi Codex credential refresh failed; check the connection or log in through Pi again");}
    if (!auth) throw new Error("Pi Codex login is missing; log in through Pi before running this Agent");
  }
  // Adapter-controlled, bounded retry waits; no hidden SDK retry multiplier.
  const settings = SettingsManager.inMemory({retry: {enabled: false, provider: {maxRetries: 0, timeoutMs: s.timeout * 1000}},
    compaction: {enabled: false, ...p.runtime_options.compaction}, enableAnalytics: false, enableInstallTelemetry: false, packages: []});
  const restored = p.checkpoint ? restoreSession(agentDir, p.cwd, p.checkpoint) : undefined;
  const manager = restored?.manager ?? SessionManager.inMemory(p.cwd);
  restoredPhase = restored?.bundle.phase;
  persistence = new SessionPersistence(manager, agentDir, () => Boolean(current?.frame.payload.checkpoint_enabled),
    () => ({application_id: current!.frame.payload.application_id, task_id: current!.frame.payload.task_id,
      run_id: current!.frame.run_id, instance_id: instance}), invoke, restored?.bundle);
  let finalDelivery = false;
  const identity = (callId: string, nativeParentId?: string | null) => ({application_id: current!.frame.payload.application_id,
    task_id: current!.frame.payload.task_id, run_id: current!.frame.run_id, instance_id: instance, call_id: callId,
    native_session_id: manager.getSessionId(), native_parent_id: nativeParentId === undefined ? manager.getLeafId() : nativeParentId});
  const selected = nativeTools(p.tools, p.cwd, invoke, identity, () => !finalDelivery, p.serial_tools, agentDir, () => {nativeIncomplete = true; session?.agent.abort();}, persistence);
  const extra = s.extra_completion_params || {};
  const searchMode = codex ? (extra.web_search || "auto") : "off";
  const loader = new DefaultResourceLoader({cwd: p.cwd, agentDir, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPrompt: codex && searchMode !== "off" ?
      `${p.instructions}\n\nNative web search is available. Use it for current information and cite the sources you use.` :
      p.tools.length ? p.instructions :
      `${p.instructions}\n\nNo tools are available in this run. Do not call or simulate tools. If the task needs unavailable information, explain the limitation.`,
    extensionFactories: [selected.extension]});
  await loader.reload();
  const {session: created} = await createAgentSession({cwd: p.cwd, agentDir, modelRuntime: runtime,
    model, thinkingLevel: codex ? (extra.reasoning_effort || "xhigh") : "off", tools: selected.tools.map(tool => tool.name),
    noTools: "all", customTools: selected.tools,
    resourceLoader: loader, settingsManager: settings, sessionManager: manager});
  enableInstructionOnlyTurns(created);
  await created.bindExtensions({onError: () => created.agent.abort()});
  modelFailure = configureModel(created, s, p.model.request_headers, async () => {
    await persistence!.save();
    const requestIdentity = identity(`model:${randomUUID()}`);
    const permit = await invoke({method: "model_prepare", identity: requestIdentity});
    if (!isDeepStrictEqual(permit.identity, requestIdentity)) throw new Error("Invalid model permission identity");
    finalDelivery = permit.state === "final";
    return {state: permit.state, agent_context: permit.agent_context, identity: requestIdentity};
  }, attempt => reportRetry?.(attempt), agentDir, invoke, codex,
  (identity, attempt, result) => reportSearch?.(identity, attempt, result));
  const nativePayload = created.agent.onPayload;
  created.agent.onPayload = async (payload, model) => {
    const result = {...(await nativePayload?.(payload, model) ?? payload) as Obj};
    const extra = s.extra_completion_params || {};
    for (const key of ["top_p", "seed"]) if (extra[key] !== undefined) result[key] = extra[key];
    if (p.tools.length) for (const key of ["tool_choice", "parallel_tool_calls"])
      if (extra[key] !== undefined) result[key] = extra[key];
    if (extra.reasoning_effort !== undefined && !codex) {
      if (api === "openai-responses") result.reasoning = {effort: extra.reasoning_effort};
      else result.reasoning_effort = extra.reasoning_effort;
    }
    const projected = {...result, ...extra.extra_body};
    const publicSchemas = new Map(p.tools.map((tool: Obj) => [tool.visible_name, tool.parameters]));
    if (Array.isArray(projected.tools)) projected.tools = projected.tools.map((tool: Obj) => {
      const name = tool.function?.name ?? tool.name;
      const parameters = publicSchemas.get(name);
      if (!parameters) return tool;
      // Pi's native schemas include optional fields. Codex rejects strict
      // function schemas unless every field is required; AgentLoom still
      // validates and authorizes the selected tool arguments before execution.
      const strict = codex ? false : tool.strict;
      return tool.function
        ? {...tool, strict, function: {...tool.function, parameters}}
        : {...tool, strict, parameters};
    });
    if (finalDelivery || outputCorrection) {
      delete projected.tools;
      delete projected.tool_choice;
      delete projected.parallel_tool_calls;
    }
    if (codex && searchMode !== "off") {
      projected.tools = [...(projected.tools || []), {type: "web_search"}];
      if (searchMode === "required") projected.tool_choice = {type: "web_search"};
    }
    if (p.output_contract) {
      if (api === "openai-responses" || codex) {
        projected.text = {...(projected.text || {}), format: {
          type: "json_schema", name: p.output_contract.name,
          schema: p.output_contract.schema, strict: true,
        }};
      } else {
        projected.response_format = {type: "json_schema", json_schema: {
          name: p.output_contract.name, schema: p.output_contract.schema,
          strict: true,
        }};
      }
    }
    return projected;
  };
  return created;
}

async function run(frame: Frame, abort: AbortController) {
  const p = frame.payload;
  outputCorrection = false;
  modelFailure = {timedOut: false, status: 0, reason: ""};
  let seq = 0;
  const event = (kind: string, payload: Obj) => write({version: 2, kind: "event", instance_id: frame.instance_id,
    run_id: frame.run_id, request_id: frame.request_id, sequence: ++seq, event: kind, payload});
  const sources = new Map<string, string>();
  reportSearch = (identity, attempt, result) => {
    for (const citation of result.citations) sources.set(citation.url, citation.title);
    event("model", {phase: "web_search", call_id: identity.call_id, attempt,
      completed_calls: result.calls, citations: result.citations});
  };
  const usage = {input_tokens: 0, output_tokens: 0, total_tokens: 0, cached_input_tokens: 0};
  let unavailableTool = false;
  let unavailableToolTurns = 0;
  let outputBudgetExhausted = false;
  let terminalRejections = 0;
  let unsubscribe: (() => void) | undefined;
  let outputValidator: ValidateFunction | undefined;
  if (p.output_contract) {
    outputValidator = new Ajv2020({
      strict: true,
      strictNumbers: true,
      allErrors: false,
      validateFormats: false,
    }).compile(p.output_contract.schema);
  }
  try {
    if (p.checkpoint && session) throw new Error("Cannot restore into an active Pi session");
    if (!p.continue_session || !session) {
      session?.dispose();
      session = await createSession(p);
    }
    if (abort.signal.aborted) throw new Error("Interrupted");
    event("run", {phase: "started", resumed: Boolean(p.checkpoint)});
    unsubscribe = session.subscribe(e => {
      if (e.type === "compaction_start") event("checkpoint", {phase: "compaction_started"});
      if (e.type === "compaction_end") event("checkpoint", {phase: "compaction_ended", aborted: e.aborted});
      if (e.type === "message_start" && e.message.role === "assistant") event("model", {phase: "started"});
      if (e.type === "message_end" && e.message.role === "assistant") {
        if (e.message.content.some(block => block.type === "toolCall" && !p.tools.some((tool: Obj) => tool.visible_name === block.name))) {
          unavailableToolTurns += 1;
          if (unavailableToolTurns >= (p.runtime_options.max_stop_attempts || 3)) {
            unavailableTool = true;
            session!.agent.abort();
          }
        }
        const u = e.message.usage;
        usage.input_tokens += u.input + u.cacheRead + u.cacheWrite;
        usage.output_tokens += u.output;
        usage.total_tokens += u.totalTokens;
        usage.cached_input_tokens += u.cacheRead;
        event("model", {phase: "completed", stop_reason: e.message.stopReason});
      }
    });
    reportRetry = attempt => event("model", {phase: "retry", attempt});
    const trigger = async (content: string | null, correction = false) => {
      if (correction) {
        await session!.sendCustomMessage({
          customType: "agentloom_output_validation",
          display: false,
          content: content || "",
        }, {triggerTurn: true});
      } else if (content === null) {
        await runInstructionOnlyTurn(session!);
      } else {
        await session!.prompt(content, {expandPromptTemplates: false});
      }
    };
    if (p.checkpoint && !p.record_task) {
      if (restoredPhase !== "complete") await session.sendCustomMessage({
        customType: "agentloom_resume", display: false,
        content: "Resume the interrupted task from the restored conversation and committed tool results. Do not repeat completed work.",
      }, {triggerTurn: true});
    } else await trigger(p.task);
    let last = session.messages.at(-1);
    let outputValidationReason = "invalid structured output";
    const correctOutput = async (message: string, disableTools = true): Promise<boolean> => {
      if (disableTools) outputCorrection = true;
      terminalRejections += 1;
      if (last?.role === "assistant" && (last.stopReason === "length" ||
          terminalRejections >= (p.runtime_options.max_stop_attempts || 3))) {
        outputBudgetExhausted = true;
        return false;
      }
      await trigger(message, true);
      last = session!.messages.at(-1);
      return true;
    };
    while (last?.role === "assistant" && last.stopReason !== "error") {
      const text = last.content.filter((b: Obj) => b.type === "text").map((b: Obj) => b.text).join("");
      const textualTool = textualToolCallName(text);
      if (textualTool) {
        outputValidationReason = "textual tool call was not executed";
        const available = p.tools.map((tool: Obj) => tool.visible_name);
        const availability = available.length ? `Available tools: ${available.join(", ")}.` : "No tools are available.";
        const selected = available.includes(textualTool);
        if (!await correctOutput(
          `Your previous response contained a textual call to ${textualTool}, which was not executed. ` +
          `${selected ? "That tool requires a real structured call." : "That tool is not available in this run."} ${availability} ` +
          "Use only actual structured tool calls for available tools. Otherwise answer with the information you have or explain the limitation.",
          false,
        )) break;
        continue;
      }
      if (!outputValidator) break;
      outputValidationReason = "invalid structured output";
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        if (!await correctOutput(
          "Your previous final output was not valid JSON. Return a corrected value matching the required JSON Schema.",
        )) break;
        continue;
      }
      if (!outputValidator(parsed)) {
        const detail = outputValidator.errors?.[0]?.message || "schema validation failed";
        if (!await correctOutput(
          `Your previous final output did not match the required JSON Schema: ${detail}. Return a corrected value.`,
        )) break;
        continue;
      }
      break;
    }
    if (nativeIncomplete || modelFailure.timedOut || unavailableTool || abort.signal.aborted ||
        (last?.role === "assistant" && last.stopReason === "aborted")) throw new Error("Interrupted");
    const state = last?.role !== "assistant" || last.stopReason === "error" ? "failed" :
      outputBudgetExhausted ? "max_steps_error" :
      last.stopReason === "length" ? "max_steps_error" : "success";
    const outputText = last?.role === "assistant" ? last.content.filter((b: Obj) => b.type === "text").map((b: Obj) => b.text).join("") : "";
    const citedText = state === "success" && sources.size && !outputValidator ? outputText + "\n\nSources:\n" +
      [...sources].map(([url, title]) => `- [${title.replace(/[\[\]\r\n]/g, " ")}](<${url}>)`).join("\n") : outputText;
    const output = outputValidator && state === "success" ? JSON.parse(outputText) : citedText;
    if (state === "success") await persistence!.save("complete");
    event("usage", usage);
    // Host emits the public terminal event only after its Stop gate.
    response(frame, {method: "run", state, terminal_rejections: terminalRejections,
      output, usage, artifacts: [], checkpoint: persistence?.latest ?? null,
      error: state === "failed" ? {category: "provider", message: modelFailure.reason || "Pi model request failed", retryable: modelFailure.status === 429 || modelFailure.status >= 500} :
        outputBudgetExhausted ? {category: "output_validation", message: `Agent exhausted its execution budget after ${outputValidationReason}`, retryable: true} : null});
  } catch (error) {
    const interrupted = abort.signal.aborted;
    const detail = error instanceof Error ? error.message : "";
    const safeDetail = detail.startsWith("Pi Codex ") || detail.startsWith("Incompatible Pi native checkpoint")
      ? detail.slice(0, 240) : "";
    response(frame, {method: "run", state: interrupted ? "interrupted" : "failed", terminal_rejections: terminalRejections,
      output: null, usage, artifacts: [], checkpoint: persistence?.latest ?? null,
      error: {category: interrupted ? "interrupted" : unavailableTool ? "output_validation" : nativeIncomplete ? "tool" : "provider", message: unavailableTool ? "Pi model repeatedly requested an unavailable tool" : nativeIncomplete ? "Pi native execution could not be durably completed" : interrupted ? "Pi run interrupted" : modelFailure.timedOut ? "Pi model request timed out" : safeDetail || modelFailure.reason || "Pi model request failed", retryable: modelFailure.timedOut}});
  } finally {
    rejectCallbacks();
    unsubscribe?.();
    current = undefined;
    reportRetry = undefined;
    reportSearch = undefined;
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
  if (frame.version !== 2 || frame.kind !== "request" || !frame.request_id?.startsWith("host:") ||
      !frame.instance_id || (instance && instance !== frame.instance_id) || seen.has(frame.request_id)) throw new Error();
  if (closing) throw new Error();
  seen.add(frame.request_id);
  const p = frame.payload;
  if (p.method === "handshake") {
    if (instance || p.protocol_version !== 2 || p.bridge_version !== 1 || p.native_tool_contract !== 1) throw new Error();
    instance = frame.instance_id;
    const [major, minor] = process.versions.node.split(".").map(Number);
    if (major < 22 || (major === 22 && minor < 19)) throw new Error();
    response(frame, {method: "handshake", runtime_id: "pi", protocol_version: 2, bridge_version: 1, sdk_version: sdkVersion,
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
    if (accepted) {current!.abort.abort(); rejectCallbacks(); session?.abortCompaction(); void session?.abort();}
    response(frame, {method: "cancel", accepted});
  } else if (p.method === "snapshot") {
    response(frame, {method: "snapshot", checkpoint: persistence?.latest ?? null});
  } else if (p.method === "close") {
    closing = true;
    current?.abort.abort();
    rejectCallbacks();
    session?.abortCompaction();
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
input.on("close", () => {current?.abort.abort(); session?.abortCompaction(); void session?.abort().finally(() => process.exit(0)); if (!session) process.exit(0);});
