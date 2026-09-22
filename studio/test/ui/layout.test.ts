import { describe, expect, test } from "bun:test"
import { SIDEBAR_WIDTH, resolveSidebarLayout } from "../../src/ui/layout"

describe("resolveSidebarLayout", () => {
  test("reserves a 42-column right sidebar on wide terminals", () => {
    expect(
      resolveSidebarLayout({
        terminalWidth: 121,
        preference: "auto",
        open: false,
      }),
    ).toEqual({
      visible: true,
      mode: "inline",
      side: "right",
      width: SIDEBAR_WIDTH,
      reservedWidth: 42,
      contentWidth: 75,
    })
  })

  test("keeps the sidebar hidden at the wide-screen boundary", () => {
    expect(
      resolveSidebarLayout({
        terminalWidth: 120,
        preference: "auto",
        open: false,
      }),
    ).toEqual({
      visible: false,
      mode: "hidden",
      side: "right",
      width: SIDEBAR_WIDTH,
      reservedWidth: 0,
      contentWidth: 116,
    })
  })

  test("opens as a right overlay without shrinking narrow content", () => {
    expect(
      resolveSidebarLayout({
        terminalWidth: 80,
        preference: "auto",
        open: true,
      }),
    ).toEqual({
      visible: true,
      mode: "overlay",
      side: "right",
      width: SIDEBAR_WIDTH,
      reservedWidth: 0,
      contentWidth: 76,
    })
  })

  test("an explicit open overrides a hidden preference", () => {
    expect(
      resolveSidebarLayout({
        terminalWidth: 160,
        preference: "hidden",
        open: true,
      }).mode,
    ).toBe("inline")
  })
})
