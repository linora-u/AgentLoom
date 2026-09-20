/** SDK-native session snapshots; the host is the durable commit barrier. */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { SessionManager } from "@earendil-works/pi-coding-agent";

type Obj = Record<string, any>;

export function restoreSession(agentDir: string, cwd: string, checkpoint: Obj) {
  const plan = JSON.parse(readFileSync(join(agentDir, "restore.json"), "utf8"));
  const bundle = plan.bundle;
  if (checkpoint.runtime_id !== "pi" || checkpoint.runtime_version !== "0.79.4" ||
      checkpoint.state_schema_version !== 1 || bundle.version !== 1 || bundle.sdk_version !== "0.79.4" ||
      bundle.session.header.version !== 3 || bundle.session.header.cwd !== cwd ||
      bundle.session.header.id !== checkpoint.payload.session_id)
    throw new Error("Incompatible Pi native checkpoint");
  const sessionPath = join(agentDir, "restored-session.jsonl");
  writeFileSync(sessionPath, [bundle.session.header, ...bundle.session.entries]
    .map(value => JSON.stringify(value)).join("\n") + "\n", {mode: 0o600, flag: "wx"});
  const manager = SessionManager.open(sessionPath, agentDir, cwd);
  // All alignment checks happen in the host before this file is produced.
  // appendMessage is the supported SDK API; no executor is involved.
  for (const message of plan.append) manager.appendMessage({...message, timestamp: Date.now()});
  return {manager, bundle};
}

export class SessionPersistence {
  readonly calls = new Map<string, Obj>();
  latest: Obj | null = null;
  private pending: Promise<void> = Promise.resolve();

  constructor(readonly manager: SessionManager, private agentDir: string,
      private enabled: () => boolean, private scope: () => Obj,
      private invoke: (payload: Obj) => Promise<Obj>, restored?: Obj) {
    for (const call of restored?.calls ?? []) this.calls.set(this.key(call.identity), call);
  }

  private key(identity: Obj) {return JSON.stringify([identity.native_parent_id, identity.call_id]);}

  register(identity: Obj, toolName: string, args: Obj, owner: string) {
    const key = this.key(identity);
    if (this.calls.has(key)) throw new Error("Duplicate session tool call");
    this.calls.set(key, {identity, tool_name: toolName, arguments: structuredClone(args), owner});
  }

  save(phase: "running" | "complete" = "running"): Promise<void> {
    if (!this.enabled()) return Promise.resolve();
    // Concurrent SDK tools share one ordered stream of checkpoints. Take the
    // snapshot when its turn reaches the queue, never overwrite newer state
    // with a delayed snapshot captured by an earlier tool.
    this.pending = this.pending.then(async () => {
      const bundle = {version: 1, sdk_version: "0.79.4", scope: this.scope(),
        session: {header: this.manager.getHeader(), entries: this.manager.getEntries()},
        calls: [...this.calls.values()], phase};
      const raw = JSON.stringify(bundle);
      const sha256 = createHash("sha256").update(raw).digest("hex");
      try {
        writeFileSync(join(this.agentDir, `session-${sha256}.json`), raw, {mode: 0o600, flag: "wx"});
      } catch (error) {
        // Content-addressed duplicates are checked with no-follow reads and
        // SHA-256 by the host. Never overwrite an existing path or symlink.
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      }
      const ack = await this.invoke({method: "session_checkpoint", sha256});
      if (ack.checkpoint?.payload.artifact !== sha256) throw new Error("Invalid session checkpoint acknowledgement");
      this.latest = ack.checkpoint;
    });
    return this.pending;
  }
}
