/**
 * AgentLoom branding uses the compact block-letter treatment of OpenCode's TUI
 * logo, redrawn for the AgentLoom name.
 *
 * Upstream inspiration: packages/tui/src/logo.ts
 * Commit: efb6cc2d4bf6332eb156709795d2b3a649198b65
 * License: MIT; see ../../upstream/LICENSE.opencode.
 */

export const AGENTLOOM_BRAND = {
  productName: "AgentLoom",
  command: "agentloom",
  terminalPrefix: "AgentLoom",
} as const

export const AGENTLOOM_LOGO = [
  "▄▀█ █▀▀ █▀▀ █▄░█ ▀█▀ █░░ █▀█ █▀█ █▀▄▀█",
  "█▀█ █▄█ ██▄ █░▀█ ░█░ █▄▄ █▄█ █▄█ █░▀░█",
] as const

export function formatTerminalTitle(context?: string): string {
  const label = context?.trim()
  if (!label) return AGENTLOOM_BRAND.terminalPrefix
  return `${AGENTLOOM_BRAND.terminalPrefix} | ${label}`
}
