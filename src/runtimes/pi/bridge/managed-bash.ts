/**
 * Pi's public BashOperations inside an OS-owned task group.
 * Adapted from Pi 1.0.0 core/tools/bash.ts and utils/shell.ts at
 * a13d35a742c6ef8462812a28fbe1d8c8b7431c32 (MIT; PI-LICENSE.txt).
 * Preserve the SDK execution contract; only task ownership and PID stopping
 * differ. The OS owner cleans descendants when the entire invocation ends.
 */
import { spawn } from "node:child_process";
import { constants as fsConstants } from "node:fs";
import { access } from "node:fs/promises";
import { constants as osConstants } from "node:os";
import { delimiter, join } from "node:path";
import { getAgentDir, getShellConfig, type BashOperations } from "@earendil-works/pi-coding-agent";
import { waitForChildProcess } from "./sdk-child-process.js";

const MAX_TIMEOUT_MS = 2_147_483_647;

function resolveTimeoutMs(timeout: number | undefined): number | undefined {
  if (timeout === undefined) return undefined;
  if (!Number.isFinite(timeout) || timeout <= 0)
    throw new Error("Invalid timeout: must be a finite number of seconds");
  const timeoutMs = timeout * 1000;
  if (timeoutMs > MAX_TIMEOUT_MS)
    throw new Error(`Invalid timeout: maximum is ${MAX_TIMEOUT_MS / 1000} seconds`);
  return timeoutMs;
}

// The SDK injects this environment through its tool definition. Preserve its
// public operations fallback too, without merging a supplied environment again.
function shellEnv(): NodeJS.ProcessEnv {
  const binDir = join(getAgentDir(), "bin");
  const pathKey = Object.keys(process.env).find(key => key.toLowerCase() === "path") ?? "PATH";
  const currentPath = process.env[pathKey] ?? "";
  const entries = currentPath.split(delimiter).filter(Boolean);
  return {...process.env, [pathKey]: entries.includes(binDir) ? currentPath :
    [binDir, currentPath].filter(Boolean).join(delimiter)};
}

export const managedBashOperations: BashOperations = {
  exec: async (command, cwd, options) => {
    const timeoutMs = resolveTimeoutMs(options.timeout);
    if (options.signal?.aborted) throw new Error("aborted");
    const config = getShellConfig();
    try { await access(cwd, fsConstants.F_OK); }
    catch { throw new Error(`Working directory does not exist: ${cwd}\nCannot execute bash commands.`); }
    const fromStdin = config.commandTransport === "stdin";
    const child = spawn(config.shell, fromStdin ? config.args : [...config.args, command], {
      cwd, env: options.env ?? shellEnv(), detached: false,
      stdio: [fromStdin ? "pipe" : "ignore", "pipe", "pipe"], windowsHide: true,
    });
    if (fromStdin) {
      child.stdin?.on("error", () => {});
      child.stdin?.end(command);
    }
    let timedOut = false;
    let timer: NodeJS.Timeout | undefined;
    // Never kill the shared Application group from a single tool. A timeout
    // remains uncertain until the outer OS owner finishes the task scope.
    const stop = () => {
      child.kill("SIGKILL");
      // Descendants can keep writing after the shell dies. Stop this tool's
      // capture on cancellation instead of letting its idle timer reset
      // forever. The caller records an uncertain effect; the OS task owner
      // still must confirm that the entire group exited before releasing it.
      child.stdout?.destroy();
      child.stderr?.destroy();
    };
    try {
      if (timeoutMs !== undefined) timer = setTimeout(() => {timedOut = true; stop();}, timeoutMs);
      child.stdout?.on("data", options.onData);
      child.stderr?.on("data", options.onData);
      if (options.signal?.aborted) stop();
      else options.signal?.addEventListener("abort", stop, {once: true});
      const exitCode = await waitForChildProcess(child);
      if (options.signal?.aborted) throw new Error("aborted");
      if (timedOut) throw new Error(`timeout:${options.timeout}`);
      const signal = child.signalCode;
      return {exitCode: exitCode ?? (signal ? 128 + (osConstants.signals[signal] ?? 0) : 1)};
    } finally {
      if (timer) clearTimeout(timer);
      options.signal?.removeEventListener("abort", stop);
    }
  },
};
