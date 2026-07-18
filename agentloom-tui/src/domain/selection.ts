export type SelectionIdentity =
  | { kind: "system"; systemID: string }
  | { kind: "run"; applicationID: string; runID: string }
  | {
      kind: "worker"
      applicationID: string
      runID: string
      agentName: string
      callIndex: number
    }

export function selectionKey(identity: SelectionIdentity) {
  if (identity.kind === "system") return join("system", identity.systemID)
  if (identity.kind === "run") return join("run", identity.applicationID, identity.runID)
  return join(
    "worker",
    identity.applicationID,
    identity.runID,
    identity.agentName,
    identity.callIndex,
  )
}

function join(...parts: Array<string | number | null>) {
  return parts
    .map((part) => {
      if (part === null) return "-"
      const value = String(part)
      return `${value.length}:${value}`
    })
    .join("|")
}
