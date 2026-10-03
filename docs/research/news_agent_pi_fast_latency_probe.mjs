/**
 * Manual, bounded latency comparison for Pi's ChatGPT subscription Codex route.
 *
 * Run with Node >=22.19 from the isolated worktree after `loom runtime install pi`:
 *   /home/lin/.nvm/versions/node/v22.23.3/bin/node \
 *     docs/research/news_agent_pi_fast_latency_probe.mjs \
 *     > docs/research/news_agent_pi_fast_latency_20261003_block2.jsonl
 *
 * The bearer token stays in this process. Output contains metrics and hashes only.
 * This probe consumes included subscription usage; it never reads an API key.
 */
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const bridge = resolve(root, ".venv/share/pi/bridge");
const aiDist = resolve(bridge, "node_modules/@earendil-works/pi-ai/dist");
const piCli = resolve(bridge, "node_modules/.bin/pi");
if (!existsSync(piCli) || !existsSync(aiDist)) {
  throw new Error("Pi is not installed in this worktree; run loom runtime install pi");
}

const { getBuiltinModel } = await import(pathToFileURL(resolve(aiDist, "providers/all.js")));
const { stream } = await import(pathToFileURL(resolve(aiDist, "api/openai-codex-responses.js")));
const token = execFileSync(process.execPath, [piCli, "auth", "print-bearer-token", "--provider", "openai-codex"], {
  encoding: "utf8",
  maxBuffer: 64 * 1024,
}).trim();
if (!token) throw new Error("Pi Codex subscription OAuth is not available");

const model = getBuiltinModel("openai-codex", "gpt-6-luna");
const expected = Array.from({ length: 240 }, (_, index) => String(index + 1)).join(", ");
const prompt = "Write the integers 1 through 240 in order, each separated by a comma and one space. No introduction or explanation.";
const defaultOrder = ["priority", "default", "default", "priority", "default", "priority", "priority", "default"];
if (process.argv.slice(2).some((argument) => argument !== "--reverse")) {
  throw new Error("The only supported option is --reverse");
}
const tiers = process.argv.includes("--reverse") ? defaultOrder.toReversed() : defaultOrder;
const hash = (value) => createHash("sha256").update(value).digest("hex");
const round = (value) => Math.round(value * 1000) / 1000;

process.stdout.write(JSON.stringify({
  type: "metadata",
  dateUtc: new Date().toISOString(),
  route: "openai-codex subscription OAuth",
  transport: "sse",
  model: model.id,
  sequence: tiers,
  promptSha256: hash(prompt),
  expectedOutputSha256: hash(expected),
  cacheRetention: "none",
  reasoningEffort: "low",
  note: "Timing is indirect Fast evidence, not a server-side allocation or billing record",
}) + "\n");

for (const [index, tier] of tiers.entries()) {
  const start = performance.now();
  let firstText = null;
  let lastText = null;
  let output = "";
  let sentTier = null;
  let responseTier = null;
  let httpStatus = null;
  let httpResponses = 0;
  let headerTiming = null;
  let serverTiming = null;
  let maxTextGap = 0;
  let textDeltaEvents = 0;
  const events = stream(
    model,
    { messages: [{ role: "user", content: prompt, timestamp: Date.now() }] },
    {
      apiKey: token,
      transport: "sse",
      serviceTier: tier,
      reasoningEffort: "low",
      cacheRetention: "none",
      sessionId: randomUUID(),
      timeoutMs: 120000,
      onPayload: (body) => {
        sentTier = body.service_tier;
        if (sentTier !== tier) throw new Error(`Request tier mismatch at trial ${index + 1}`);
        return body;
      },
      onResponse: (response) => {
        httpResponses += 1;
        httpStatus = response.status;
        headerTiming = response.headers["openai-processing-ms"] ?? null;
        serverTiming = response.headers["server-timing"] ?? null;
      },
      onProviderStreamEvent: (event) => {
        if (event?.type === "response.completed" || event?.type === "response.done") {
          responseTier = event.response?.service_tier ?? null;
        }
      },
    },
  );
  for await (const event of events) {
    if (event.type === "text_delta") {
      const now = performance.now();
      if (firstText === null) firstText = now;
      if (lastText !== null) maxTextGap = Math.max(maxTextGap, now - lastText);
      lastText = now;
      textDeltaEvents += 1;
      output += event.delta ?? "";
    }
  }
  const result = await events.result();
  const end = performance.now();
  const outputTokens = result.usage?.output ?? result.usage?.outputTokens ?? null;
  process.stdout.write(JSON.stringify({
    type: "trial",
    trial: index + 1,
    tier,
    sentTier,
    responseTier,
    httpStatus,
    httpResponses,
    headerTiming,
    serverTiming,
    stopReason: result.stopReason,
    inputTokens: result.usage?.input ?? null,
    outputTokens,
    reasoningTokens: result.usage?.reasoning ?? null,
    cacheReadTokens: result.usage?.cacheRead ?? null,
    textChars: output.length,
    textDeltaEvents,
    maxTextGapSeconds: round(maxTextGap / 1000),
    outputMatches: output.trim() === expected,
    outputSha256: hash(output.trim()),
    firstTextSeconds: firstText === null ? null : round((firstText - start) / 1000),
    totalSeconds: round((end - start) / 1000),
    visibleCharsPerSecond: firstText === null ? null : round(output.length / ((end - firstText) / 1000)),
  }) + "\n");
  if (result.stopReason !== "stop" || output.trim() !== expected) {
    throw new Error(`Invalid completion at trial ${index + 1}`);
  }
}
