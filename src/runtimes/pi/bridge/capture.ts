/** Capture the first authorized executor result before SDK display truncation. */
import { constants } from "node:fs";
import { access, readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { createHash } from "node:crypto";
import { managedBashOperations } from "./managed-bash.js";
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition,
  createBashToolDefinition, createLocalBashOperations, type ToolDefinition } from "@earendil-works/pi-coding-agent";

// Pinned published SDK helper preserves its image sniffing; no copied image algorithm.
const mime = await import(new URL("./utils/mime.js", import.meta.resolve("@earendil-works/pi-coding-agent")).href);
type Obj = Record<string, any>;

export function capturedExecutor(name: string, cwd: string, args: Obj, containsPrivate?: (value: unknown) => boolean) {
  let bytes: Buffer | undefined;
  let imageMime: string | null = null;
  let uncertain = false;
  let privateRejected = false;
  const chunks: Buffer[] = [];
  const expected = args.path === undefined ? undefined : resolve(cwd, args.path);
  const checkPath = (actual: string) => {
    if (actual !== expected) throw new Error("SDK path differs from authorized path");
  };
  const operations = {
    access: async (path: string) => {checkPath(path); await access(path, name === "edit" ? constants.R_OK | constants.W_OK : constants.R_OK);},
    readFile: async (path: string) => {checkPath(path); return bytes ??= await readFile(path);},
    writeFile: async (path: string, content: string) => {checkPath(path); await writeFile(path, content, "utf8");},
    mkdir: async (path: string) => {
      if (path !== dirname(expected!)) throw new Error("SDK directory differs from authorized path");
      await mkdir(path, {recursive: true});
    },
    detectImageMimeType: async (path: string) => {
      imageMime = mime.detectSupportedImageMimeType(await operations.readFile(path));
      return imageMime;
    },
  };
  const local = process.env.AGENTLOOM_MANAGED_PROCESS_GROUP ? managedBashOperations : createLocalBashOperations();
  const tool: ToolDefinition<any, any> = name === "read" ? createReadToolDefinition(cwd, {operations}) :
    name === "edit" ? createEditToolDefinition(cwd, {operations}) :
    name === "write" ? createWriteToolDefinition(cwd, {operations}) :
    createBashToolDefinition(cwd, {operations: {exec: async (command, directory, options) => {
      try {
        const result = await local.exec(command, directory,
          {...options, onData: data => {
            chunks.push(Buffer.from(data));
            // The SDK can spill incremental bash output to disk. In a private
            // Harness run, inspect the joined stream before its first onData.
            if (!containsPrivate) options.onData(data);
          }});
        if (containsPrivate) {
          const complete = Buffer.concat(chunks);
          privateRejected = containsPrivate(complete.toString("utf8"));
          if (!privateRejected) options.onData(complete);
        }
        if (!Number.isInteger(result.exitCode)) {uncertain = true; throw new Error("Shell exit status is unknown");}
        return result;
      } catch (error) {
        // Without a returned integer exit code the effect may have happened,
        // even when the SDK reports the process failure as an exception.
        uncertain = true;
        throw error;
      }
    }}});
  return {tool, isPrivateRejected: () => privateRejected, isUncertain: () => uncertain, save: async (authorization: Obj, result: any, agentDir: string) => {
    let raw: any = result ?? null;
    let format = "json";
    if (name === "read" && bytes) {
      if (imageMime) raw = {mime_type: imageMime, base64: bytes.toString("base64")};
      else {
        const lines = bytes.toString("utf8").split("\n");
        const start = args.offset ? Math.max(0, args.offset - 1) : 0;
        raw = lines.slice(start, args.limit === undefined ? undefined : Math.min(start + args.limit, lines.length)).join("\n");
        format = "text";
      }
    } else if (name === "bash") {raw = Buffer.concat(chunks).toString("utf8"); format = "text";}
    // The SDK reports exitCode, not stream EOF. Its idle drain may close pipes
    // held by background descendants; never infer complete coverage from exit 0.
    privateRejected = privateRejected || Boolean(containsPrivate?.([raw, result]));
    const value = JSON.stringify({raw_output: privateRejected ? null : raw,
      result: privateRejected ? null : result ?? null, format: privateRejected ? "json" : format,
      complete: privateRejected ? false : name === "bash" ? null : true,
      limitations: privateRejected ? ["Tool response contained a bound credential; execution effect is uncertain"] : name === "bash" ? ["SDK does not expose stream EOF; background descendant output may be missing"] : [],
      display_truncated: Boolean(result?.details?.truncation?.truncated)});
    const filename = `capture-${authorization.authorization_id}.json`;
    await writeFile(resolve(agentDir, filename), value, {encoding: "utf8", mode: 0o600, flag: "wx"});
    return {sha256: createHash("sha256").update(value).digest("hex")};
  }};
}
