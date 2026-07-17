# OpenCode TUI provenance

AgentLoom's terminal presentation layer is a deliberately reduced downstream
adaptation of the OpenCode TUI, not a runtime dependency on OpenCode.

- Upstream repository: <https://github.com/anomalyco/opencode>
- Upstream commit: `efb6cc2d4bf6332eb156709795d2b3a649198b65`
- Upstream package: `packages/tui`
- Upstream license: MIT, preserved in [LICENSE.opencode](LICENSE.opencode)
- Imported on: 2026-07-17

## Derived source map

| AgentLoom file | OpenCode source | Adaptation |
| --- | --- | --- |
| `src/app/run.tsx` | `packages/tui/src/app.tsx` and `packages/tui/src/util/renderer.ts` | Retains OpenTUI renderer lifecycle, alternate-screen cleanup, terminal title handling, and mouse-enabled launch while removing OpenCode services, plugins, sessions, and coding tools. |
| `src/app/view.tsx` | `packages/tui/src/app.tsx`, `packages/tui/src/routes/session/index.tsx`, and `packages/tui/src/routes/session/sidebar.tsx` | Reduces the session UI to a Builder conversation, responsive Agent/Run directory, and detail view. |
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

Upstream changes are reviewed and manually adapted at this presentation seam.
Do not copy OpenCode SDK, provider, session, coding-agent, or plugin-host state
into these modules. When importing another substantial OpenCode source file:

1. record the new upstream commit here and in `opencode.commit`;
2. preserve its source path in the derived source map;
3. retain `LICENSE.opencode`; and
4. keep AgentLoom behavior covered through its public pure-function interface.
