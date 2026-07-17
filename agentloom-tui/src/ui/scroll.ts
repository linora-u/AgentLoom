/**
 * Default TUI scroll behavior adapted from OpenCode's util/scroll.ts.
 *
 * Upstream commit: efb6cc2d4bf6332eb156709795d2b3a649198b65
 * License: MIT; see ../../upstream/LICENSE.opencode.
 */

import type { ScrollAcceleration } from "@opentui/core"

export const DEFAULT_SCROLL_SPEED = 3

export class FixedSpeedScroll implements ScrollAcceleration {
  constructor(private readonly speed: number) {}

  tick(): number {
    return this.speed
  }

  reset(): void {}
}

export function createDefaultScrollAcceleration(): ScrollAcceleration {
  return new FixedSpeedScroll(DEFAULT_SCROLL_SPEED)
}
