# Use an independent, explicit, sequential Hook Runtime

## Status

Accepted.

## Decision

AgentLoom treats Skill and Hook as independent modules. `SKILL.md` cannot
declare Hooks. Shell Hooks enter the runtime only through a top-level `hooks:`
mapping or an explicitly referenced Bundle containing `HOOK.yaml`; directories
are never auto-discovered. Both sources compile through one validator into an
immutable Hook Plan, while every Agent invocation owns an isolated Hook Run.

The external YAML interface is an event map of single Shell entries. Every
entry has a stable ID. Global, application, and Agent layers resolve by ID with
full replacement or `enabled: false` tombstones; partial field merging is
forbidden. Matching Handlers execute sequentially, and `PreToolUse`
transformations are passed to the next Handler.

Only eleven events with production emitters remain. `PreToolUse` and `Stop`
are fail-closed gates; all other events are fail-open observers. The Shell wire
format is versioned JSON on stdin and one strict `HookResult` JSON object on
stdout. Prompt, HTTP, Agent, asynchronous, once-only, and response-rewrite Hook
types are outside this contract.

Core tool validation is not a configurable Hook. Tool execution is fixed as
configured transforms, final strict decoding, `CoreToolGuard`, final-input
recording, side effect, and outcome observation. A blocked invocation has a
typed blocked outcome and is not reported as tool failure.

Configuring or referencing a Shell Hook is explicit authorization to execute
trusted local code. The runtime filters sensitive inherited environment values
and terminates process trees on timeout, but does not expose misleading
`allow-network` or `allow-scripts` flags without a real operating-system
sandbox.

## Consequences

- Skill discovery and loading have no Hook side effects or Hook dependencies.
- Hook configuration retains source provenance instead of relying on generic
  deep-merge output.
- Stable ordering and diagnostics are reproducible from the Hook Plan
  fingerprint.
- Existing Skill Hooks must migrate to independent Bundles; no compatibility
  registration layer is retained.
