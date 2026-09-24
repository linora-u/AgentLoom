import { Ajv2020 } from "ajv/dist/2020.js";
import type { ValidateFunction } from "ajv";
import { isDeepStrictEqual } from "node:util";
import type { SessionPersistence } from "./checkpoint.js";

export type Obj = Record<string, any>;
export type BridgeInvoke = (payload: Obj) => Promise<Obj>;
export type Identity = (id: string, nativeParentId?: string | null) => Obj;

export type NativeToolsOptions = {
  manifest: Obj[];
  cwd: string;
  invoke: BridgeInvoke;
  identity: Identity;
  canUseTools: () => boolean;
  serialTools: string[];
  persistence: SessionPersistence;
};

export class BridgeToolCoordinator {
  private readonly permits = new Map<string, Obj>();
  private readonly seen = new Set<string>();
  private readonly active = new Map<string, string>();
  private readonly serial: Set<string>;
  private readonly selected: Map<string, Obj>;
  private readonly validators: Map<string, ValidateFunction>;
  private readonly ajv = new Ajv2020({strict: true, coerceTypes: false, useDefaults: false});

  constructor(private readonly options: NativeToolsOptions) {
    this.serial = new Set(options.serialTools);
    this.selected = new Map(options.manifest.map(tool => [tool.visible_name, tool]));
    this.validators = new Map(options.manifest
      .filter(tool => tool.owner === "runtime")
      .map(tool => [tool.visible_name, this.ajv.compile(tool.parameters)]));
  }

  callKey(parentId: string | null, callId: string) {
    return JSON.stringify([parentId, callId]);
  }

  isSerial(entry: Obj) {
    return this.serial.has(entry.visible_name);
  }

  executionMode(entry: Obj) {
    return this.isSerial(entry) ? "sequential" as const : "parallel" as const;
  }

  internalParameters(entry: Obj) {
    return this.isSerial(entry)
      ? {type: "object", additionalProperties: true}
      : entry.parameters;
  }

  hasPermit(key: string) {
    return this.permits.has(key);
  }

  permit(key: string) {
    return this.permits.get(key);
  }

  assistantCall(callId: string): {entry: Obj; call: Obj} {
    for (const entry of [...this.options.persistence.manager.getEntries()].reverse()) {
      if (entry.type !== "message" || entry.message.role !== "assistant") continue;
      const call = entry.message.content.find((part: Obj) => part.type === "toolCall" && part.id === callId);
      if (call) return {entry, call};
    }
    throw new Error("Pi serial tool call is missing its assistant anchor");
  }

  consumePermit(callId: string) {
    const key = this.active.get(callId);
    const permit = key === undefined ? undefined : this.permits.get(key);
    if (key !== undefined) {
      this.active.delete(callId);
      this.permits.delete(key);
    }
    return permit;
  }

  registerAssistantBatch(content: Obj[]) {
    const parentId = this.options.persistence.manager.getLeafId();
    const batch = new Set<string>();
    for (const part of content) {
      if (part.type !== "toolCall") continue;
      const key = this.callKey(parentId, part.id);
      if (!this.options.canUseTools() || this.seen.has(key) || batch.has(part.id)
          || this.active.has(part.id) || !this.selected.has(part.name)) {
        this.permits.clear();
        this.active.clear();
        throw new Error("Unselected or duplicate tool call");
      }
      batch.add(part.id);
      this.seen.add(key);
      this.active.set(part.id, key);
    }
    const deferBatch = content.some(
      part => part.type === "toolCall" && this.serial.has(part.name),
    );
    return {parentId, deferBatch};
  }

  async prepare(part: Obj, nativeParentId?: string | null) {
    const entry = this.selected.get(part.name)!;
    const callIdentity = this.options.identity(part.id, nativeParentId);
    const key = this.callKey(callIdentity.native_parent_id, part.id);
    if (entry.owner !== "runtime") {
      const prepared = await this.options.invoke({method: "platform_prepare", identity: callIdentity,
        tool_name: part.name, arguments: part.arguments});
      part.arguments = prepared.arguments;
      this.options.persistence.register(callIdentity, part.name, part.arguments, entry.owner);
      this.permits.set(key, {platform: true, identity: callIdentity, arguments: structuredClone(part.arguments),
        rejection: prepared.rejection});
      return;
    }
    const prepared = await this.options.invoke({method: "tool_prepare", call: {identity: callIdentity, tool: entry,
      cwd: this.options.cwd, raw_arguments: part.arguments}});
    const grant = prepared.authorization;
    const validator = this.validators.get(part.name);
    if (grant && (!isDeepStrictEqual(grant.identity, callIdentity) || !isDeepStrictEqual(grant.tool, entry)
        || grant.cwd !== this.options.cwd || !validator?.(grant.final_arguments)))
      throw new Error("Invalid native authorization");
    this.permits.set(key, prepared);
    if (grant) part.arguments = grant.final_arguments;
    this.options.persistence.register(callIdentity, part.name, part.arguments, entry.owner);
  }

  recoverPreparedRejection(message: Obj): {message: Obj} | undefined {
    const {entry} = this.assistantCall(message.toolCallId);
    const key = this.callKey(entry.parentId, message.toolCallId);
    const permit = this.permits.get(key);
    if (!permit?.rejection) return undefined;
    if (permit.rejection.call_id !== message.toolCallId || permit.rejection.tool_name !== message.toolName)
      throw new Error("AgentLoom rejection identity mismatch");
    this.active.delete(message.toolCallId);
    this.permits.delete(key);
    const text = permit.rejection.model_content || "AgentLoom preparation rejected";
    return {message: {...message, content: [{type: "text", text}], details: {}, isError: true}};
  }
}
