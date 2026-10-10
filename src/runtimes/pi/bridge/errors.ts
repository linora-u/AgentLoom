/** Public errors expose only a known failure class and a provider request UUID. */
export function providerError(detail: unknown) {
  const raw = typeof detail === "string" ? detail : "";
  // The Codex SDK can wrap the same provider processing error. Normalize only
  // its exact known prefix; arbitrary messages remain non-retryable and private.
  const message = raw.startsWith("Codex error: ") ? raw.slice("Codex error: ".length) : raw;
  const processing = message.startsWith("An error occurred while processing your request.");
  // Undici can terminate an SSE body after partial model output. The SDK retries
  // first; preserve that exact transport class if its retries are exhausted.
  const transport = raw === "terminated";
  const transient = processing || transport;
  const requestId = processing ? message.match(/request ID ([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\b/i)?.[1] : undefined;
  return {category: "provider", retryable: transient,
    message: "Pi model request failed" + (processing ? " (transient provider processing error" +
      (requestId ? `; request ID ${requestId}` : "") + ")" :
      transport ? " (transient provider transport error)" : "")};
}
