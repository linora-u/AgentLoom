/**
 * Responsive sidebar rules derived from OpenCode's session TUI layout.
 *
 * Upstream: packages/tui/src/routes/session/index.tsx
 * Commit: efb6cc2d4bf6332eb156709795d2b3a649198b65
 * License: MIT; see ../../upstream/LICENSE.opencode.
 */

export const SIDEBAR_WIDTH = 42
export const SIDEBAR_WIDE_BREAKPOINT = 120
export const CONTENT_HORIZONTAL_INSET = 4

export type SidebarPreference = "auto" | "hidden"
export type SidebarMode = "hidden" | "inline" | "overlay"

export type SidebarLayout = {
  visible: boolean
  mode: SidebarMode
  side: "right"
  width: typeof SIDEBAR_WIDTH
  reservedWidth: number
  contentWidth: number
}

export function resolveSidebarLayout(input: {
  terminalWidth: number
  preference: SidebarPreference
  open: boolean
}): SidebarLayout {
  const terminalWidth = Math.max(0, Math.floor(input.terminalWidth))
  const wide = terminalWidth > SIDEBAR_WIDE_BREAKPOINT
  const visible = input.open || (input.preference === "auto" && wide)
  const mode = !visible ? "hidden" : wide ? "inline" : "overlay"
  const reservedWidth = mode === "inline" ? SIDEBAR_WIDTH : 0

  return {
    visible,
    mode,
    side: "right",
    width: SIDEBAR_WIDTH,
    reservedWidth,
    contentWidth: Math.max(0, terminalWidth - reservedWidth - CONTENT_HORIZONTAL_INSET),
  }
}
