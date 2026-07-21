# OpenCode TUI provenance

AgentLoom Application Studio embeds the pinned OpenCode Runtime and adapts its
TUI interaction semantics at the AgentLoom presentation boundary.

- Upstream repository: <https://github.com/anomalyco/opencode>
- Upstream release: `v1.18.3`
- Upstream commit: `127bdb30784d508cc556c71a0f32b508a3061517`
- Upstream package: `packages/tui`
- Upstream license: MIT, preserved in [LICENSE.opencode](LICENSE.opencode)
- Reviewed on: 2026-07-21

## Derived source map

| AgentLoom file | OpenCode source | Adaptation |
| --- | --- | --- |
| `src/app/run.tsx` | `packages/tui/src/app.tsx` and `packages/tui/src/util/renderer.ts` | Retains OpenTUI renderer lifecycle, alternate-screen cleanup, terminal title handling, and mouse-enabled launch while removing OpenCode services, plugins, sessions, and coding tools. |
| `src/app/view.tsx` | `packages/tui/src/app.tsx`, `packages/tui/src/routes/session/index.tsx`, and `packages/tui/src/routes/session/sidebar.tsx` | Retains the conversation scrollbox, sticky-bottom behavior, PageUp/PageDown half-viewport scrolling, responsive Application directory, and detail view. |
| `src/app/session.ts` | `packages/tui/src/context/sync.tsx` and `packages/tui/src/component/prompt/index.tsx` | Keeps session status and turn completion isolated so an interrupted request cannot settle over a newer request. |
| `src/studio/opencode-studio.ts` | `packages/tui/src/component/prompt/index.tsx`, `packages/opencode/src/session/prompt.ts`, `packages/opencode/src/session/message-v2.ts`, and `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts` | Uses OpenCode abort, Task-child cancellation, and the official message-deletion boundary. Studio removes only the unfinished turn from future model context without reverting file changes, then makes the next user message a fresh loop objective. |
| `src/ui/layout.ts` | `packages/tui/src/routes/session/index.tsx` and `packages/tui/src/routes/session/sidebar.tsx` | Retains the `>120` wide breakpoint, 42-column right sidebar, and narrow overlay behavior as pure layout data. |
| `src/ui/theme.ts` | `packages/tui/src/theme/assets/opencode.json` | Retains the semantic dark/light palette under AgentLoom names. |
| `src/ui/brand.ts` | `packages/tui/src/logo.ts` | Retains the compact block-letter treatment but redraws the mark and all labels for AgentLoom. |
| `script/build.ts` | `packages/opencode/script/build.ts` | Reduces OpenCode's multi-target release builder to one native standalone binary while retaining the OpenTUI Solid transform and compiled-runtime settings. |

`src/ui/status.ts` is AgentLoom-specific. It uses the semantic colors from the
derived theme but maps AgentLoom bridge states rather than OpenCode session,
provider, LSP, or MCP states.

The npm package is private and marked `UNLICENSED`; that metadata does not
replace or restrict OpenCode's MIT grant for the mapped derived portions. The
verbatim upstream MIT notice remains in `LICENSE.opencode`.

## Update policy

The `@opencode-ai/sdk` and `opencode-ai` packages are locked to the release
recorded above. Upstream changes are reviewed and adapted at the presentation
and AgentLoom-domain seams. When importing another substantial behavior:

1. record the new upstream commit here and in `opencode.commit`;
2. preserve its source path in the derived source map;
3. retain `LICENSE.opencode`; and
4. keep AgentLoom behavior covered through its public pure-function interface.
