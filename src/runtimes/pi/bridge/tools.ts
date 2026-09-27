/** Official SDK executors guarded by AgentLoom's existing native tool contract. */
import { capturedExecutor } from "./capture.js";
import type { SessionPersistence } from "./checkpoint.js";
import { isDeepStrictEqual } from "node:util";
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, createBashToolDefinition, type ExtensionFactory, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { BridgeToolCoordinator, type BridgeInvoke, type Identity, type Obj } from "./tool-coordinator.js";

export function nativeTools(manifest: Obj[], cwd: string, invoke: BridgeInvoke, identity: Identity, canUseTools: () => boolean, serialTools: string[], agentDir: string, failRun: () => void, persistence: SessionPersistence) {
  const context = new BridgeToolCoordinator({manifest, cwd, invoke, identity, canUseTools, serialTools,
    persistence});
  const tools: ToolDefinition<any, any>[] = manifest.map(entry => {
    if (entry.owner !== "runtime") {
      if (entry.operation === "write" || entry.operation === "shell") throw new Error("Unsupported platform tool");
      return {name: entry.visible_name, label: entry.visible_name, description: entry.parameters.description || entry.capability,
        parameters: context.internalParameters(entry), executionMode: context.executionMode(entry),
        execute: async (callId: string, args: any) => {
          const permit = context.consumePermit(callId);
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
          if (record.status !== "completed") throw new Error(record.model_content || "AgentLoom tool failed");
          return {content: [{type: "text" as const, text: typeof completed.model_output === "string" ? completed.model_output : JSON.stringify(completed.model_output)}],
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
    return {...official, parameters: context.internalParameters(entry), prepareArguments: undefined, renderCall: undefined, renderResult: undefined,
      executionMode: context.executionMode(entry),
      execute: async (callId: string, args: any, signal: AbortSignal | undefined, onUpdate: any, ctx: any) => {
        const permit = context.consumePermit(callId);
        if (!permit?.authorization || !isDeepStrictEqual(permit.authorization.final_arguments, args) ||
            permit.authorization.tool.visible_name !== entry.visible_name) throw new Error("Missing native authorization");
        try {await persistence.save();} catch (error) {failRun(); throw error;}
        const dispatched = await invoke({method: "tool_dispatch", authorization: permit.authorization});
        if (!dispatched.authorization) throw new Error(dispatched.rejection?.model_content || "Native dispatch rejected");
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
          if (settled.record.status !== "completed") throw new Error(settled.record.model_content || "Native tool failed");
          return settled.record.output;
        } finally {
          if (!committed) failRun();
        }
      },
    };
  });
  const extension: ExtensionFactory = pi => {
    pi.on("session_before_compact", async () => {
      try {await persistence.save();} catch (error) {failRun(); throw error;}
    });
    pi.on("session_compact", async () => {
      try {await persistence.save();} catch (error) {failRun(); throw error;}
    });
    const onMessageEnd = async ({message}: any) => {
      if (message.role === "toolResult") return context.recoverPreparedRejection(message);
      if (message.role !== "assistant") return;
      const replacement = structuredClone(message);
      const {parentId, deferBatch} = context.registerAssistantBatch(replacement.content);
      const parallel: Promise<void>[] = [];
      for (const part of replacement.content) {
        if (part.type !== "toolCall") continue;
        if (!deferBatch && context.hasSelectedTool(part.name)) parallel.push(context.prepare(part, parentId));
      }
      await Promise.all(parallel);
      return {message: replacement};
    };
    pi.on("message_end", onMessageEnd);
    pi.on("tool_call", async ({toolCallId, toolName, input}) => {
      const {entry, call} = context.assistantCall(toolCallId);
      const key = context.callKey(entry.parentId, toolCallId);
      if (!context.hasPermit(key)) {
        await context.prepare(call, entry.parentId);
        const mutable = input as Obj;
        for (const key of Object.keys(mutable)) delete mutable[key];
        Object.assign(mutable, structuredClone(call.arguments));
      }
      await persistence.save();
      const permit = context.permit(key);
      if (permit?.platform) {
        if (permit.rejection) return {block: true, reason: permit.rejection.model_content || "AgentLoom preparation rejected"};
        if (isDeepStrictEqual(permit.arguments, input)) return;
      }
      if (!permit?.authorization || permit.authorization.tool.visible_name !== toolName ||
          !isDeepStrictEqual(permit.authorization.final_arguments, input))
        return {block: true, reason: permit?.rejection?.model_content || "Missing or mismatched AgentLoom authorization"};
    });
  };
  return {tools, extension};
}
