export type OpenCodeSessionInfo = {
  id: string
  title: string
  directory?: string
  metadata?: Record<string, unknown>
  permission?: OpenCodePermissionRule[]
}

export type OpenCodePermissionRule = {
  permission: string
  pattern: string
  action: "allow" | "ask" | "deny"
}

export interface OpenCodeSessionApi {
  workspaceKey?: string
  list(): Promise<OpenCodeSessionInfo[]>
  create(input: {
    title: string
    metadata: Record<string, unknown>
    permission: OpenCodePermissionRule[]
  }): Promise<OpenCodeSessionInfo>
}

export class ApplicationStudioSessions {
  constructor(private readonly api: OpenCodeSessionApi) {}

  async open(applicationID: string): Promise<OpenCodeSessionInfo> {
    const existing = (await this.api.list()).find((session) => (
      applicationIDFromMetadata(session.metadata) === applicationID
      && belongsToWorkspace(session, this.api.workspaceKey)
    ))
    if (existing) return existing

    return this.api.create({
      title: `AgentLoom · ${applicationID}`,
      metadata: {
        agentloom: {
          kind: "application-studio",
          application_id: applicationID,
          ...(this.api.workspaceKey ? { workspace: this.api.workspaceKey } : {}),
        },
      },
      permission: applicationOnlyPermissions(applicationID, this.api.workspaceKey),
    })
  }

  async openNew(): Promise<OpenCodeSessionInfo> {
    return this.api.create({
      title: "AgentLoom · New Application",
      metadata: {
        agentloom: {
          kind: "application-studio-new",
          creation_id: crypto.randomUUID(),
          ...(this.api.workspaceKey ? { workspace: this.api.workspaceKey } : {}),
        },
      },
      permission: newApplicationPermissions(),
    })
  }
}

function belongsToWorkspace(session: OpenCodeSessionInfo, workspaceKey: string | undefined): boolean {
  if (!workspaceKey) return true
  const agentloom = session.metadata?.agentloom
  if (agentloom && typeof agentloom === "object" && !Array.isArray(agentloom)) {
    const stored = (agentloom as Record<string, unknown>).workspace
    if (typeof stored === "string") return stored === workspaceKey
  }
  // Sessions created before workspace metadata existed remain resumable only
  // when OpenCode reports that their persisted directory is this workspace.
  return session.directory === workspaceKey
}

export function applicationOnlyPermissions(
  applicationID: string,
  workspaceKey?: string,
): OpenCodePermissionRule[] {
  const applicationPath = `applications/${applicationID}`
  const workspacePaths = workspaceKey
    ? workspaceEditPatterns(workspaceKey, applicationPath)
    : []
  return [
    { permission: "edit", pattern: "*", action: "ask" },
    { permission: "edit", pattern: applicationPath, action: "allow" },
    { permission: "edit", pattern: `${applicationPath}/*`, action: "allow" },
    ...workspacePaths.flatMap((path) => [
      { permission: "edit", pattern: path, action: "allow" as const },
      { permission: "edit", pattern: `${path}/*`, action: "allow" as const },
    ]),
    { permission: "bash", pattern: "*", action: "ask" },
    { permission: "external_directory", pattern: "*", action: "ask" },
    { permission: "agentloom_run", pattern: "*", action: "ask" },
  ]
}

function workspaceEditPatterns(workspaceKey: string, applicationPath: string): string[] {
  const root = workspaceKey.replaceAll("\\", "/").replace(/\/$/, "")
  const absolute = `${root}/${applicationPath}`
  // OpenCode 1.18.x reports edit permission targets relative to worktree. For
  // non-Git projects that worktree is `/`, so the same in-project file is
  // reported without its leading slash (for example `private/var/...`).
  const rootRelative = absolute.replace(/^\/+/, "")
  return rootRelative === absolute ? [absolute] : [absolute, rootRelative]
}

export function newApplicationPermissions(): OpenCodePermissionRule[] {
  return [
    { permission: "edit", pattern: "*", action: "ask" },
    { permission: "bash", pattern: "*", action: "ask" },
    { permission: "external_directory", pattern: "*", action: "ask" },
    { permission: "agentloom_run", pattern: "*", action: "ask" },
  ]
}

function applicationIDFromMetadata(metadata: Record<string, unknown> | undefined): string | null {
  const agentloom = metadata?.agentloom
  if (!agentloom || typeof agentloom !== "object" || Array.isArray(agentloom)) return null
  const value = agentloom as Record<string, unknown>
  if (value.kind !== "application-studio") return null
  return typeof value.application_id === "string" ? value.application_id : null
}
