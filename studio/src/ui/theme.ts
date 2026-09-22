/**
 * AgentLoom's semantic palette is derived from OpenCode's default theme.
 *
 * Upstream: packages/tui/src/theme/assets/opencode.json
 * Commit: efb6cc2d4bf6332eb156709795d2b3a649198b65
 * License: MIT; see ../../upstream/LICENSE.opencode.
 */

export type ThemeMode = "dark" | "light"
export type ThemeTone = "primary" | "secondary" | "accent" | "error" | "warning" | "success" | "info" | "muted"

export type AgentLoomPalette = {
  primary: string
  secondary: string
  accent: string
  error: string
  warning: string
  success: string
  info: string
  text: string
  muted: string
  background: string
  panel: string
  element: string
  border: string
  borderActive: string
}

export const AGENTLOOM_THEME = {
  dark: {
    primary: "#fab283",
    secondary: "#5c9cf5",
    accent: "#9d7cd8",
    error: "#e06c75",
    warning: "#f5a742",
    success: "#7fd88f",
    info: "#56b6c2",
    text: "#eeeeee",
    muted: "#808080",
    background: "#0a0a0a",
    panel: "#141414",
    element: "#1e1e1e",
    border: "#484848",
    borderActive: "#606060",
  },
  light: {
    primary: "#3b7dd8",
    secondary: "#7b5bb6",
    accent: "#d68c27",
    error: "#d1383d",
    warning: "#d68c27",
    success: "#3d9a57",
    info: "#318795",
    text: "#1a1a1a",
    muted: "#8a8a8a",
    background: "#ffffff",
    panel: "#fafafa",
    element: "#f5f5f5",
    border: "#b8b8b8",
    borderActive: "#a0a0a0",
  },
} as const satisfies Record<ThemeMode, AgentLoomPalette>

export function themeFor(mode: ThemeMode): AgentLoomPalette {
  return AGENTLOOM_THEME[mode]
}

export function toneColor(tone: ThemeTone, mode: ThemeMode): string {
  return AGENTLOOM_THEME[mode][tone]
}
