/** Public errors expose only a known failure class and a provider request UUID. */
export function providerError(detail: unknown) {
  const message = typeof detail === "string" ? detail : "";
  const processing = message.startsWith("An error occurred while processing your request.");
  // Undici can terminate an SSE body after partial model output. The SDK retries
  // first; preserve that exact transport class if its retries are exhausted.
  const transport = message === "terminated";
  const transient = processing || transport;
  const requestId = processing ? message.match(/request ID ([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\b/i)?.[1] : undefined;
  return {category: "provider", retryable: transient,
    message: "Pi model request failed" + (processing ? " (transient provider processing error" +
      (requestId ? `; request ID ${requestId}` : "") + ")" :
      transport ? " (transient provider transport error)" : "")};
}
