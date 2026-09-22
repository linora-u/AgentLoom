import { describe, expect, test } from "bun:test"
import { shouldReduceMotion } from "../../src/ui/motion"

describe("terminal motion capability", () => {
  test("uses static status symbols for explicit, dumb, and CI terminals", () => {
    expect(shouldReduceMotion({ AGENTLOOM_REDUCED_MOTION: "1" })).toBe(true)
    expect(shouldReduceMotion({ AGENTLOOM_REDUCED_MOTION: "true" })).toBe(true)
    expect(shouldReduceMotion({ TERM: "dumb" })).toBe(true)
    expect(shouldReduceMotion({ CI: "true" })).toBe(true)
  })

  test("keeps state-driven animation on capable terminals", () => {
    expect(shouldReduceMotion({ TERM: "xterm-256color" })).toBe(false)
  })
})
