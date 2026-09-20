/** Official SDK executors guarded by AgentLoom's existing native tool contract. */
import { capturedExecutor } from "./capture.js";
import type { SessionPersistence } from "./checkpoint.js";
import { isDeepStrictEqual } from "node:util";
import { Ajv2020 } from "ajv/dist/2020.js";
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, createBashToolDefinition, type ExtensionFactory, type ToolDefinition } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
type Callback = (payload: Obj) => Promise<Obj>;
type Identity = (id: string, nativeParentId?: string | null) => Obj;

export function nativeTools(manifest: Obj[], cwd: string, invoke: Callback, identity: Identity, canUseTools: () => boolean, serialTools: string[], agentDir: string, failRun: () => void, persistence: SessionPersistence) {
  const permits = new Map<string, Obj>();
  const seen = new Set<string>();
  const active = new Map<string, string>();
  const serial = new Set(serialTools);
  const selected = new Map(manifest.map(tool => [tool.visible_name, tool]));
  const ajv = new Ajv2020({strict: true, coerceTypes: false, useDefaults: false});
  const validators = new Map(manifest.filter(tool => tool.owner === "runtime").map(tool => [tool.visible_name, ajv.compile(tool.parameters)]));
  const callKey = (parentId: string | null, callId: string) => JSON.stringify([parentId, callId]);
  const internalParameters = (entry: Obj) => serial.has(entry.visible_name)
    ? {type: "object", additionalProperties: true}
    : entry.parameters;
  const tools: ToolDefinition<any, any>[] = manifest.map(entry => {
    if (entry.owner !== "runtime") {
      if (entry.operation === "write" || entry.operation === "shell") throw new Error("Unsupported platform tool");
      return {name: entry.visible_name, label: entry.visible_name, description: entry.parameters.description || entry.capability,
        parameters: internalParameters(entry), executionMode: serial.has(entry.visible_name) ? "sequential" : "parallel",
        execute: async (callId: string, args: any) => {
          const key = active.get(callId);
          const permit = key === undefined ? undefined : permits.get(key);
          if (key !== undefined) {
            active.delete(callId);
            permits.delete(key);
          }
          if (!permit?.platform || !isDeepStrictEqual(permit.arguments, args))
            throw new Error("Missing platform tool preparation");
          let completed: Obj;
          try {
            await persistence.save();
            completed = await invoke({method: "platform_invoke", identity: permit.identity,
              tool_name: entry.visible_name, arguments: args});
            if (!completed.record || completed.record.call_id !== callId || completed.record.tool_name !== entry.visible_name)
              throw new Error("Platform receipt identity mismatch");
          } catch (error) {failRun(); throw error;}
          const record = completed.record;
          if (record.status !== "completed") throw new Error(record.error?.message || "AgentLoom tool failed");
          return {content: [{type: "text" as const, text: typeof record.output === "string" ? record.output : JSON.stringify(record.output)}],
            details: {agentloom: record}};
        },
      };
    }
    if (entry.owner !== "runtime" || entry.provider !== "pi" || !((entry.visible_name === "read" && entry.operation === "read") || (["write", "edit"].includes(entry.visible_name) && entry.operation === "write") || (entry.visible_name === "bash" && entry.operation === "shell")))
      throw new Error("Unsupported native tool");
    const official: ToolDefinition<any, any> = entry.visible_name === "read" ? createReadToolDefinition(cwd) : entry.visible_name === "bash" ? createBashToolDefinition(cwd) : entry.visible_name === "edit" ? createEditToolDefinition(cwd) : createWriteToolDefinition(cwd);
    const parameters = {...official.parameters, additionalProperties: false};
    // No lossy schema projection, silent aliases or alternate basic implementation.
    if (!isDeepStrictEqual(JSON.parse(JSON.stringify(parameters)), entry.parameters)) throw new Error("Native tool schema mismatch");
    return {...official, parameters: internalParameters(entry), prepareArguments: undefined, renderCall: undefined, renderResult: undefined,
      executionMode: serial.has(entry.visible_name) ? "sequential" : "parallel",
      execute: async (callId: string, args: any, signal: AbortSignal | undefined, onUpdate: any, ctx: any) => {
        const key = active.get(callId);
        const permit = key === undefined ? undefined : permits.get(key);
        if (key !== undefined) {
          active.delete(callId);
          permits.delete(key);
        }
        if (!permit?.authorization || !isDeepStrictEqual(permit.authorization.final_arguments, args) ||
            permit.authorization.tool.visible_name !== entry.visible_name) throw new Error("Missing native authorization");
        try {await persistence.save();} catch (error) {failRun(); throw error;}
        const dispatched = await invoke({method: "tool_dispatch", authorization: permit.authorization});
        if (!dispatched.authorization) throw new Error(dispatched.rejection?.error?.message || "Native dispatch rejected");
        if (!isDeepStrictEqual(dispatched.authorization, permit.authorization)) throw new Error("Native dispatch authorization mismatch");
        const grant = dispatched.authorization;
        let committed = false;
        try {
          const captured = capturedExecutor(entry.visible_name, cwd, args);
          let result: unknown;
          let error: Obj | null = null;
          try {
            if (signal?.aborted) throw new Error("Interrupted");
            result = await captured.tool.execute(callId, args, signal, onUpdate, ctx);
          } catch {
            error = {kind: "NativeToolError", message: "Pi native tool execution failed", stage: "tool_execution", retryable: false};
          }
          const uncertain = captured.isUncertain() || Boolean(signal?.aborted);
          if (uncertain && !error) error = {kind: "NativeToolError", message: "Pi native execution outcome is uncertain", stage: "tool_execution", retryable: false};
          const capture = await captured.save(grant, result, agentDir);
          const settled = await invoke({method: "tool_settle", capture, outcome: {identity: grant.identity,
            authorization_id: grant.authorization_id, status: uncertain ? "uncertain" : error ? "error" : "completed",
            output: null, error}});
          if (settled.state !== "committed" || !settled.commit_id || !isDeepStrictEqual(settled.identity, grant.identity) ||
              settled.authorization_id !== grant.authorization_id) throw new Error("Native result not durably committed");
          committed = true;
          if (settled.record.status !== "completed") throw new Error(settled.record.error?.message || "Native tool failed");
          return settled.record.output;
        } finally {
          if (!committed) failRun();
        }
      },
    };
  });
  const extension: ExtensionFactory = pi => {
    const assistantCall = (callId: string): {entry: Obj; call: Obj} => {
      for (const entry of [...persistence.manager.getEntries()].reverse()) {
        if (entry.type !== "message" || entry.message.role !== "assistant") continue;
        const call = entry.message.content.find((part: Obj) => part.type === "toolCall" && part.id === callId);
        if (call) return {entry, call};
      }
      throw new Error("Pi serial tool call is missing its assistant anchor");
    };
    const prepare = async (part: Obj, nativeParentId?: string | null) => {
      const entry = selected.get(part.name)!;
      const callIdentity = identity(part.id, nativeParentId);
      const key = callKey(callIdentity.native_parent_id, part.id);
      if (entry.owner !== "runtime") {
        const prepared = await invoke({method: "platform_prepare", identity: callIdentity,
          tool_name: part.name, arguments: part.arguments});
        part.arguments = prepared.arguments;
        persistence.register(callIdentity, part.name, part.arguments, entry.owner);
        permits.set(key, {platform: true, identity: callIdentity, arguments: structuredClone(part.arguments),
          rejection: prepared.rejection});
        return;
      }
      const prepared = await invoke({method: "tool_prepare", call: {identity: callIdentity, tool: entry, cwd,
        raw_arguments: part.arguments}});
      const grant = prepared.authorization;
      if (grant && (!isDeepStrictEqual(grant.identity, callIdentity) || !isDeepStrictEqual(grant.tool, entry) ||
          grant.cwd !== cwd || !validators.get(part.name)!(grant.final_arguments)))
        throw new Error("Invalid native authorization");
      permits.set(key, prepared);
      if (grant) part.arguments = grant.final_arguments;
      persistence.register(callIdentity, part.name, part.arguments, entry.owner);
    };
    pi.on("session_before_compact", async () => {
      try {await persistence.save();} catch (error) {failRun(); throw error;}
    });
    pi.on("session_compact", async () => {
      try {await persistence.save();} catch (error) {failRun(); throw error;}
    });
    pi.on("message_end", async ({message}) => {
      if (message.role === "toolResult") {
        const {entry} = assistantCall(message.toolCallId);
        const key = callKey(entry.parentId, message.toolCallId);
        const permit = permits.get(key);
        if (!permit?.rejection) return;
        if (permit.rejection.call_id !== message.toolCallId ||
            permit.rejection.tool_name !== message.toolName)
          throw new Error("AgentLoom rejection identity mismatch");
        active.delete(message.toolCallId);
        permits.delete(key);
        const text = permit.rejection.error?.message || "AgentLoom preparation rejected";
        return {message: {...message, content: [{type: "text", text}], details: {}, isError: true}};
      }
      if (message.role !== "assistant") return;
      const replacement = structuredClone(message);
      const parentId = persistence.manager.getLeafId();
      const batch = new Set<string>();
      for (const part of replacement.content) {
        if (part.type !== "toolCall") continue;
        const key = callKey(parentId, part.id);
        if (!canUseTools() || seen.has(key) || batch.has(part.id) || active.has(part.id) || !selected.has(part.name)) {
          permits.clear();
          active.clear();
          throw new Error("Unselected or duplicate tool call");
        }
        batch.add(part.id);
        seen.add(key);
        active.set(part.id, key);
      }
      const deferBatch = replacement.content.some(
        (part: Obj) => part.type === "toolCall" && serial.has(part.name),
      );
      const parallel: Promise<void>[] = [];
      for (const part of replacement.content) {
        if (part.type !== "toolCall") continue;
        if (!deferBatch) parallel.push(prepare(part, parentId));
      }
      await Promise.all(parallel);
      return {message: replacement};
    });
    pi.on("tool_call", async ({toolCallId, toolName, input}) => {
      const {entry, call} = assistantCall(toolCallId);
      const key = callKey(entry.parentId, toolCallId);
      if (!permits.has(key)) {
        await prepare(call, entry.parentId);
        const mutable = input as Obj;
        for (const key of Object.keys(mutable)) delete mutable[key];
        Object.assign(mutable, structuredClone(call.arguments));
      }
      await persistence.save();
      const permit = permits.get(key);
      if (permit?.platform) {
        if (permit.rejection) return {block: true, reason: permit.rejection.error?.message || "AgentLoom preparation rejected"};
        if (isDeepStrictEqual(permit.arguments, input)) return;
      }
      if (!permit?.authorization || permit.authorization.tool.visible_name !== toolName ||
          !isDeepStrictEqual(permit.authorization.final_arguments, input))
        return {block: true, reason: permit?.rejection?.error?.message || "Missing or mismatched AgentLoom authorization"};
    });
  };
  return {tools, extension};
}
