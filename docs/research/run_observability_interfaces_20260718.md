# Agent runtime observability interfaces

Date: 2026-07-18

## Question and conclusion

The question is not whether AgentLoom needs more log statements. It is whether a caller can identify a run and locate its persisted evidence without scraping terminal output or guessing which directory is newest.

AgentLoom already has the durable half: `RuntimeContext` owns a per-run directory containing `manifest.json`, `logs/runtime.log`, audit data, and artifacts. The missing half was the caller contract: `run_app()` returned only the final answer. That forced batch programs to redirect stdout/stderr into a second log such as `outputs/validation_logs/*.log`.

The comparable projects consistently separate human diagnostics from machine-readable run/session interfaces. Therefore AgentLoom should expose its existing canonical run identity and a small lifecycle protocol; it should not build another logger.

## Source comparison

The local source snapshots and revisions reviewed were:

- Hermes Agent `29e3983fa879186b2122bd6779a2deb266f4acc5`
- OpenCode `efb6cc2d4bf6332eb156709795d2b3a649198b65`
- Pi `1aa3c02d56635ec40e7c8448d7eff35022e95740`
- Claude Code snapshot `4b9d30f7953273e567a18eb819f4eddd45fcc877`
- Headroom `0fa337f64f08452acec857a4ef4dfba82de589ca`

### Hermes Agent

Hermes has centralized, rotating diagnostic files (`agent.log`, `errors.log`, component logs) and injects a session ID into human log lines. Separately, its TUI gateway reserves stdout for JSON-RPC, returns an ID from `session.create`, accepts `prompt.submit`, and emits session-scoped `message.start`, `message.delta`, and `message.complete` events. The two mechanisms solve different problems: logs support operators, while the protocol lets software correlate a turn.

Sources: [logging design and session context](https://github.com/NousResearch/hermes-agent/blob/29e3983fa879186b2122bd6779a2deb266f4acc5/hermes_logging.py#L1-L29), [stdout reserved for JSON-RPC](https://github.com/NousResearch/hermes-agent/blob/29e3983fa879186b2122bd6779a2deb266f4acc5/tui_gateway/server.py#L247-L258), [`session.create`](https://github.com/NousResearch/hermes-agent/blob/29e3983fa879186b2122bd6779a2deb266f4acc5/tui_gateway/server.py#L5519), [`message.start`](https://github.com/NousResearch/hermes-agent/blob/29e3983fa879186b2122bd6779a2deb266f4acc5/tui_gateway/server.py#L9358), [`message.delta`](https://github.com/NousResearch/hermes-agent/blob/29e3983fa879186b2122bd6779a2deb266f4acc5/tui_gateway/server.py#L9484), and [`message.complete`](https://github.com/NousResearch/hermes-agent/blob/29e3983fa879186b2122bd6779a2deb266f4acc5/tui_gateway/server.py#L9612).

### OpenCode

OpenCode writes a human log under its global data directory and optionally mirrors logs to stderr with `--print-logs`. Programmatic callers do not discover work through that file. They create a session and receive its ID, subscribe to an SSE event endpoint, filter events by `sessionID`, and stop when that session becomes idle. The CLI itself uses that SDK flow and can emit line-delimited JSON carrying the session ID.

Sources: [file/stderr logger split](https://github.com/anomalyco/opencode/blob/efb6cc2d4bf6332eb156709795d2b3a649198b65/packages/core/src/observability/logging.ts#L49-L68), [`--print-logs`](https://github.com/anomalyco/opencode/blob/efb6cc2d4bf6332eb156709795d2b3a649198b65/packages/opencode/src/index.ts#L49-L68), [JSON output format](https://github.com/anomalyco/opencode/blob/efb6cc2d4bf6332eb156709795d2b3a649198b65/packages/opencode/src/cli/cmd/run.ts#L174), [SSE event API](https://github.com/anomalyco/opencode/blob/efb6cc2d4bf6332eb156709795d2b3a649198b65/packages/opencode/src/server/routes/instance/httpapi/groups/event.ts#L8-L28), [CLI session creation](https://github.com/anomalyco/opencode/blob/efb6cc2d4bf6332eb156709795d2b3a649198b65/packages/opencode/src/cli/cmd/run.ts#L519-L570), and [session-filtered event loop](https://github.com/anomalyco/opencode/blob/efb6cc2d4bf6332eb156709795d2b3a649198b65/packages/opencode/src/cli/cmd/run.ts#L697-L829).

### Pi

Pi exposes lifecycle events directly in its agent API through `Agent.subscribe()`. Its RPC mode uses strict JSONL on stdin/stdout, forwards session events as JSON records, and exposes both `sessionId` and `sessionFile` through `get_state`. Session history is persisted separately as JSONL. This is the closest match for AgentLoom's local Python API plus CLI transport.

Sources: [`Agent.subscribe()` contract](https://github.com/earendil-works/pi/blob/1aa3c02d56635ec40e7c8448d7eff35022e95740/packages/agent/src/agent.ts#L222-L234), [RPC JSONL transport](https://github.com/earendil-works/pi/blob/1aa3c02d56635ec40e7c8448d7eff35022e95740/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L1-L55), [event forwarding and session state](https://github.com/earendil-works/pi/blob/1aa3c02d56635ec40e7c8448d7eff35022e95740/packages/coding-agent/src/modes/rpc/rpc-mode.ts#L350-L455), [strict JSONL framing](https://github.com/earendil-works/pi/blob/1aa3c02d56635ec40e7c8448d7eff35022e95740/packages/coding-agent/src/modes/rpc/jsonl.ts#L1-L24).

### Claude Code source snapshot

The inspected repository explicitly identifies itself as an unofficial security-research snapshot, so it is corroborating evidence rather than a design authority. The snapshot nevertheless shows the same boundary: `--output-format=stream-json` emits line-delimited SDK records, a stdout guard protects that machine channel from non-JSON writes, SDK records carry `session_id`, and hook inputs carry both `session_id` and `transcript_path`.

Sources: [snapshot provenance warning](https://github.com/jarmuine/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/README.md#claude-code-source-snapshot-for-security-research), [NDJSON stdout contract](https://github.com/jarmuine/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/utils/streamJsonStdoutGuard.ts#L29-L47), [session/transcript fields](https://github.com/jarmuine/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/entrypoints/sdk/coreSchemas.ts#L386-L404).

### Headroom

Headroom is not an agent runtime. Its primary SDK surface wraps provider clients to compress messages before a completion request and returns the provider's response. Its proxy/SSE utilities describe provider transport, not an Application run identity or lifecycle. It is therefore not a relevant template for AgentLoom run observability.

Source: [OpenAI client wrapper](https://github.com/chopratejas/headroom/blob/0fa337f64f08452acec857a4ef4dfba82de589ca/sdk/typescript/src/adapters/openai.ts#L16-L66).

## AgentLoom decision

The implemented boundary follows the smallest common denominator required by current callers:

1. Keep `runtime.log` and the run directory as the single durable source of human diagnostics.
2. Add a structured Python result containing `application_id`, `task_id`, `run_id`, and canonical run/manifest/log paths, while retaining `run_app() -> str` as a compatibility adapter. Failures after allocation carry the same `RunInfo` through typed exceptions.
3. For an allocated run, emit only `run.started` and exactly one terminal event through an optional sink and CLI JSONL mode. A preflight rejection emits only `run.rejected` and does not allocate a run.
4. Reserve JSONL stdout for protocol records and send console diagnostics to stderr.
5. Make validation batches audit the `log_path` announced by the run instead of writing another copy under `outputs/validation_logs`.

HTTP/SSE is intentionally out of scope. OpenCode needs it because it exposes a long-lived server; AgentLoom's immediate consumer is a local Python/subprocess caller. Model-token and tool-call event schemas are also out of scope: the current requirement is reliable run correlation, and the canonical runtime log already supplies human tool-flow evidence. Those larger protocols should be introduced only when a concrete replay or live-UI consumer requires them.
