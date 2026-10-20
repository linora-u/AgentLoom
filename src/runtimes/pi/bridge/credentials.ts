/** Process-local implementation of the SDK's public CredentialStore interface. */
import type {Credential, CredentialStore} from "@earendil-works/pi-ai";

export function memoryCredentials(initial: Record<string, Credential>, remember?: (value: Credential | undefined) => void): CredentialStore {
  const values = structuredClone(initial);
  for (const value of Object.values(values)) remember?.(value);
  let pending: Promise<unknown> = Promise.resolve();
  const serialized = <T>(operation: () => Promise<T>): Promise<T> => {
    const result = pending.then(operation, operation);
    pending = result.then(() => undefined, () => undefined);
    return result;
  };
  return {
    read: async provider => structuredClone(values[provider]),
    list: async () => Object.entries(values).map(([providerId, value]) => ({providerId, type:value.type})),
    modify: (provider, update) => serialized(async () => {
      const next = await update(structuredClone(values[provider]));
      if (next !== undefined) {
        values[provider] = structuredClone(next);
        remember?.(next);
      }
      return structuredClone(values[provider]);
    }),
    delete: provider => serialized(async () => {delete values[provider];}),
  };
}
