// Transport for the public OpenClaw process SDK. All tree cleanup is upstream.
import { appendFileSync, closeSync, fsyncSync, openSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const [runtimeRoot, job, python] = process.argv.slice(2);
function atomicJson(name, value) {
  const pending = join(job, `.pending-${randomUUID()}`);
  const descriptor = openSync(pending, "wx", 0o600);
  try {
    writeFileSync(descriptor, JSON.stringify(value));
    fsyncSync(descriptor);
  } finally { closeSync(descriptor); }
  renameSync(pending, join(job, name));
  const directory = openSync(job, "r");
  try { fsyncSync(directory); } finally { closeSync(directory); }
}
function trace(event, fields = {}) {
  appendFileSync(join(job, "process-trace.jsonl"), JSON.stringify({
    event, pid: process.pid, time_ns: process.hrtime.bigint().toString(), ...fields,
  }) + "\n", { mode: 0o600 });
}
const controller = new AbortController();
function stop(reason) {
  // Diagnostics cannot prevent delivery of cancellation to the library.
  try { trace(reason); } catch { /* Keep cleanup alive when tracing fails. */ }
  controller.abort();
}
process.stdin.on("end", () => stop("executor_channel_eof"));
process.stdin.on("error", () => stop("executor_channel_error"));
process.on("SIGTERM", () => stop("transport_cancelled"));
process.on("SIGINT", () => stop("transport_cancelled"));
process.stdin.resume();

try {
  const sdk = createRequire(join(runtimeRoot, "package.json"));
  const { runCommandWithTimeout, withCommandProcessScope } = await import(
    pathToFileURL(sdk.resolve("openclaw/plugin-sdk/process-runtime"))
  );
  trace("transport_started");
  const result = await withCommandProcessScope(() => runCommandWithTimeout([
    python, "-m", "agentloom.execution.durable.job_entry", job,
  ], {
    // Never give the task the executor's control descriptor. Its separate
    // input pipe closes immediately; business admission uses authorized.json.
    input: "", signal: controller.signal,
    killProcessTree: true, requireProcessTreeExtinction: true,
    outputCapture: "discard", killGraceMs: 300,
    onOutputChunk: (chunk, stream) => appendFileSync(join(job, `${stream}.log`), chunk, { mode: 0o600 }),
  }), controller.signal);
  if (!["normal", "cooperative", "forced"].includes(result.cleanup)) {
    throw new Error("OpenClaw did not certify process-tree cleanup");
  }
  atomicJson("openclaw-cleanup.json", {
    engine_version: "2026.10.3", code: result.code, signal: result.signal,
    cleanup: result.cleanup, termination: result.termination,
  });
  // The public scope has joined the command and its process-tree cleanup.
  atomicJson("exited.json", { physical_exit_confirmed: true, owner: "openclaw" });
  trace("task_scope_exited");
} catch (error) {
  atomicJson("openclaw-cleanup-failed.json", {
    error_type: error.name, code: error.code, message: error.message,
    cleanup: error.cleanup,
  });
  process.exitCode = 1;
} finally {
  process.stdin.destroy();
}
