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
  update(
    sessionID: string,
    input: {
      title: string
      metadata: Record<string, unknown>
      permission: OpenCodePermissionRule[]
    },
  ): Promise<OpenCodeSessionInfo>
}

export type StudioSessionTarget =
  | { type: "new" }
  | { type: "application"; applicationID: string }

export type StudioPermissionMode = "application_only" | "full_access"

export class ApplicationStudioSessions {
  constructor(private readonly api: OpenCodeSessionApi) {}

  async open(
    applicationID: string,
    permissionMode: StudioPermissionMode = "application_only",
  ): Promise<OpenCodeSessionInfo> {
    const existing = (await this.api.list()).find((session) => (
      applicationIDFromMetadata(session.metadata) === applicationID
      && belongsToWorkspace(session, this.api.workspaceKey)
    ))
    if (existing) {
      return this.retarget(
        existing.id,
        { type: "application", applicationID },
        permissionMode,
      )
    }

    return this.createFresh({ type: "application", applicationID }, permissionMode)
  }

  openNew(permissionMode: StudioPermissionMode = "application_only"): Promise<OpenCodeSessionInfo> {
    return this.createFresh({ type: "new" }, permissionMode)
  }

  createFresh(
    target: StudioSessionTarget,
    permissionMode: StudioPermissionMode = "application_only",
  ): Promise<OpenCodeSessionInfo> {
    return this.api.create(sessionProperties(target, permissionMode, this.api.workspaceKey))
  }

  retarget(
    sessionID: string,
    target: StudioSessionTarget,
    permissionMode: StudioPermissionMode = "application_only",
  ): Promise<OpenCodeSessionInfo> {
    return this.api.update(
      sessionID,
      sessionProperties(target, permissionMode, this.api.workspaceKey),
    )
  }
}

function sessionProperties(
  target: StudioSessionTarget,
  permissionMode: StudioPermissionMode,
  workspaceKey?: string,
): {
  title: string
  metadata: Record<string, unknown>
  permission: OpenCodePermissionRule[]
} {
  if (target.type === "new") {
    return {
      title: "AgentLoom · New Application",
      metadata: {
        agentloom: {
          kind: "application-studio-new",
          creation_id: crypto.randomUUID(),
          ...(workspaceKey ? { workspace: workspaceKey } : {}),
        },
      },
      permission: permissionMode === "full_access"
        ? fullAccessPermissions()
        : newApplicationPermissions(),
    }
  }
  return {
    title: `AgentLoom · ${target.applicationID}`,
    metadata: {
      agentloom: {
        kind: "application-studio",
        application_id: target.applicationID,
        ...(workspaceKey ? { workspace: workspaceKey } : {}),
      },
    },
    permission: permissionMode === "full_access"
      ? fullAccessPermissions()
      : applicationOnlyPermissions(target.applicationID, workspaceKey),
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

export function fullAccessPermissions(): OpenCodePermissionRule[] {
  return [{ permission: "*", pattern: "*", action: "allow" }]
}

function applicationIDFromMetadata(metadata: Record<string, unknown> | undefined): string | null {
  const agentloom = metadata?.agentloom
  if (!agentloom || typeof agentloom !== "object" || Array.isArray(agentloom)) return null
  const value = agentloom as Record<string, unknown>
  if (value.kind !== "application-studio") return null
  return typeof value.application_id === "string" ? value.application_id : null
}
