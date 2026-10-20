/** Bound credentials stay in this process; generated content may never echo them. */
import type {Credential} from "@earendil-works/pi-ai";

export function credentialGuard(initial: unknown) {
  const enabled = Array.isArray(initial);
  const values = new Set<string>(enabled ? initial.filter(value => typeof value === "string" && value) : []);
  const remember = (credential: Credential | undefined) => {
    if (!enabled || !credential) return;
    const item = credential as unknown as Record<string, unknown>;
    for (const name of ["access", "refresh", "key", "api_key"])
      if (typeof item[name] === "string" && item[name]) values.add(item[name] as string);
  };
  const contains = (value: unknown): boolean => {
    if (!enabled) return false;
    if (typeof value === "string") {
      if ([...values].some(secret => value.includes(secret))) return true;
      if (/^\s*[\[{]/.test(value)) {
        try { return contains(JSON.parse(value)); } catch { /* Ordinary text is not JSON. */ }
      }
      return false;
    }
    if (Array.isArray(value)) return value.some(contains);
    if (value && typeof value === "object")
      return Object.entries(value).some(([key, part]) => contains(key) || contains(part));
    return false;
  };
  return {enabled, remember, contains};
}
