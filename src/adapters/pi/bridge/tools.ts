/** Official SDK executors guarded by AgentLoom's existing native tool contract. */
import { isDeepStrictEqual } from "node:util";
import { Ajv2020 } from "ajv/dist/2020.js";
import { createReadToolDefinition, type ExtensionFactory, type ToolDefinition } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;
type Callback = (payload: Obj) => Promise<Obj>;

export function nativeTools(manifest: Obj[], cwd: string, invoke: Callback, identity: (id: string) => Obj, canUseTools: () => boolean, serialTools: string[]) {
  const permits = new Map<string, Obj>();
  const seen = new Set<string>();
  const serial = new Set(serialTools);
  const selected = new Map(manifest.map(tool => [tool.visible_name, tool]));
  const ajv = new Ajv2020({strict: true, coerceTypes: false, useDefaults: false});
  const validators = new Map(manifest.filter(tool => tool.owner === "runtime").map(tool => [tool.visible_name, ajv.compile(tool.parameters)]));
  const tools: ToolDefinition<any, any>[] = manifest.map(entry => {
    if (entry.owner !== "runtime") {
      if (entry.operation === "write" || entry.operation === "shell") throw new Error("Unsupported platform tool");
      return {name: entry.visible_name, label: entry.visible_name, description: entry.parameters.description || entry.capability,
        parameters: entry.parameters, executionMode: serial.has(entry.visible_name) ? "sequential" : "parallel",
        execute: async (callId: string, args: any) => {
          const record = permits.get(callId)?.record;
          permits.delete(callId);
          if (!record || record.call_id !== callId || record.tool_name !== entry.visible_name ||
              !isDeepStrictEqual(record.input, args)) throw new Error("Missing platform tool receipt");
          if (record.status !== "completed") throw new Error(record.error?.message || "AgentLoom tool failed");
          return {content: [{type: "text" as const, text: typeof record.output === "string" ? record.output : JSON.stringify(record.output)}],
            details: {agentloom: record}};
        },
      };
    }
    if (entry.owner !== "runtime" || entry.provider !== "pi" || entry.visible_name !== "read" || entry.operation !== "read")
      throw new Error("Unsupported native tool");
    const official = createReadToolDefinition(cwd);
    const parameters = {...official.parameters, additionalProperties: false};
    // No lossy schema projection, silent aliases or alternate basic implementation.
    if (!isDeepStrictEqual(JSON.parse(JSON.stringify(parameters)), entry.parameters)) throw new Error("Native tool schema mismatch");
    return {...official, parameters, prepareArguments: undefined, renderCall: undefined, renderResult: undefined,
      executionMode: serial.has(entry.visible_name) ? "sequential" : "parallel",
      execute: async (callId: string, args: any, signal: AbortSignal | undefined, onUpdate: any, ctx: any) => {
        const permit = permits.get(callId);
        permits.delete(callId);
        if (!permit?.authorization || !isDeepStrictEqual(permit.authorization.final_arguments, args) ||
            permit.authorization.tool.visible_name !== entry.visible_name) throw new Error("Missing native authorization");
        const grant = permit.authorization;
        let result: unknown;
        let error: Obj | null = null;
        try {
          if (signal?.aborted) throw new Error("Interrupted");
          result = await official.execute(callId, args, signal, onUpdate, ctx);
        } catch {
          error = {kind: "NativeToolError", message: "Pi native tool execution failed", stage: "tool_execution", retryable: false};
        }
        const settled = await invoke({method: "tool_settle", outcome: {identity: grant.identity,
          authorization_id: grant.authorization_id, status: error ? "error" : "completed", output: error ? null : result, error}});
        if (settled.state !== "committed" || !settled.commit_id || !isDeepStrictEqual(settled.identity, grant.identity) ||
            settled.authorization_id !== grant.authorization_id) throw new Error("Native result not durably committed");
        if (settled.record.status !== "completed") throw new Error(settled.record.error?.message || "Native tool failed");
        return settled.record.output;
      },
    };
  });
  const extension: ExtensionFactory = pi => {
    pi.on("message_end", async ({message}) => {
      if (message.role !== "assistant") return;
      const replacement = structuredClone(message);
      const batch = new Set<string>();
      for (const part of replacement.content) {
        if (part.type !== "toolCall") continue;
        if (!canUseTools() || seen.has(part.id) || batch.has(part.id) || !selected.has(part.name)) {
          permits.clear();
          throw new Error("Unselected or duplicate tool call");
        }
        batch.add(part.id);
      }
      for (const id of batch) seen.add(id);
      const prepare = async (part: typeof replacement.content[number]) => {
        if (part.type !== "toolCall") return;
        permits.set(part.id, {});
        const entry = selected.get(part.name)!;
        const callIdentity = identity(part.id);
        if (entry.owner !== "runtime") {
          const completed = await invoke({method: "platform_invoke", identity: callIdentity,
            tool_name: part.name, arguments: part.arguments});
          if (completed.record.call_id !== part.id || completed.record.tool_name !== part.name)
            throw new Error("Platform receipt identity mismatch");
          permits.set(part.id, completed);
          part.arguments = completed.record.input;
          return;
        }
        const prepared = await invoke({method: "tool_prepare", call: {identity: callIdentity, tool: entry, cwd,
          raw_arguments: part.arguments}});
        const grant = prepared.authorization;
        if (grant && (!isDeepStrictEqual(grant.identity, callIdentity) || !isDeepStrictEqual(grant.tool, entry) ||
            grant.cwd !== cwd || !validators.get(part.name)!(grant.final_arguments)))
          throw new Error("Invalid native authorization");
        permits.set(part.id, prepared);
        if (grant) part.arguments = grant.final_arguments;
      };
      let parallel: Promise<void>[] = [];
      for (const part of replacement.content) {
        if (part.type !== "toolCall") continue;
        if (serial.has(part.name)) {
          await Promise.all(parallel);
          parallel = [];
          await prepare(part);
        } else parallel.push(prepare(part));
      }
      await Promise.all(parallel);
      return {message: replacement};
    });
    pi.on("tool_call", ({toolCallId, toolName, input}) => {
      const permit = permits.get(toolCallId);
      if (permit?.record && permit.record.tool_name === toolName && isDeepStrictEqual(permit.record.input, input)) {
        if (permit.record.status !== "completed") return {block: true, reason: permit.record.error?.message || "AgentLoom tool rejected"};
        return;
      }
      if (!permit?.authorization || permit.authorization.tool.visible_name !== toolName ||
          !isDeepStrictEqual(permit.authorization.final_arguments, input))
        return {block: true, reason: permit?.rejection?.error?.message || "Missing or mismatched AgentLoom authorization"};
    });
  };
  return {tools, extension};
}
