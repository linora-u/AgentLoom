import { describe, expect, test } from "bun:test"
import { AGENTLOOM_THEME } from "../../src/ui/theme"

describe("OpenCode-derived AgentLoom theme", () => {
  test("keeps the OpenCode semantic dark palette", () => {
    expect(AGENTLOOM_THEME.dark).toEqual({
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
    })
  })

  test("keeps the OpenCode semantic light palette", () => {
    expect(AGENTLOOM_THEME.light).toEqual({
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
    })
  })
})
