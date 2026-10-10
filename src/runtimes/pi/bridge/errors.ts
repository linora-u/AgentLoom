/** Public errors expose only a known failure class and a provider request UUID. */
export function providerError(detail: unknown) {
  const message = typeof detail === "string" ? detail : "";
  const transient = message.startsWith("An error occurred while processing your request.");
  const requestId = transient ? message.match(/request ID ([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\b/i)?.[1] : undefined;
  return {category: "provider", retryable: transient,
    message: "Pi model request failed" + (transient ? " (transient provider processing error" +
      (requestId ? `; request ID ${requestId}` : "") + ")" : "")};
}
