# Pi runtime (tickets 07, 09, 10)

This adapter runs real `@earendil-works/pi-coding-agent` **0.87.1** AgentSessions.
Python owns Application identity, receipts, Hook Run/Stop and resource cleanup;
Pi owns its in-memory conversation and native provider protocol. No smol model,
Tool, Todo, final_answer implementation or message format is required.

## Install and run

Node **22.19+** with npm is required. Use uv as the Python environment and
installation entry point:

```sh
uv run --locked loom runtime install pi
```

This downloads the published SDK using `npm ci --ignore-scripts`, then builds
AgentLoom's bridge and verifies its imports. `package.json` fixes the SDK at
**0.87.1**; `package-lock.json` fixes transitive versions and integrity hashes.
uv manages Python dependencies; npm installs this Node package. No SDK source
checkout, global Pi command, or manual npm build is needed.

The SDK is installed in `bridge/node_modules/`; generated JavaScript is in
`bridge/dist/`. Both remain local and ignored by Git. Only our adapter/bridge
source and dependency declarations are committed. The repository-root `pi/`
reference checkout is not used by the production runtime or installer.

Repeated installation reuses a successful result unless the source, schema or
lock changes. Concurrent installers share a process lock; failures leave no
success marker and can be retried with the same command. Install before starting
Applications; Node/npm and network access are needed for the initial download.
The equivalent Python entry point is `python -m agentloom.runtimes.pi.install`.

Use the existing `config/llm.yaml`, then select Pi in an Application YAML definition:

```yaml
name: direct_answer
agent_runtime: pi
description: Answer the user's question directly.
system_prompt: Give a concise answer.
task: Explain what AgentLoom Applications do.
tools: []
toolsets: []
```

Checkpoint is optional for this Application; enable it when task resume is
required. Run with the existing
`loom run applications/<app>/workflows/root.yaml` or `execute_app` entry.
Dependency installation is explicit; Application execution never runs npm.
Python packages include our bridge sources, schema and lock, not `node_modules`
or build artifacts. The same installer prepares the package's Pi directory.
Pi-only dependency profiles and final clean-install acceptance remain ticket 13.
The installer fingerprints every bridge TypeScript source, the schema and locks;
adding a helper file also invalidates an old build. The downloaded SDK stays in
the adapter's ignored dependency directory and is never vendored into Git.

## Selected tools and artifacts

Select official native tools explicitly with `tools: [{name: read}, {name: edit},
{name: write}, {name: bash}]`, or the `pi_read`, `pi_edit`, `pi_write`, `pi_bash`
toolsets. AgentLoom authorizes final Hook-adjusted arguments and rechecks them
immediately before the official SDK executor runs. Read an existing file first,
then wait for that result before editing or overwriting it. File history and
Shell command policies use the shared NativeToolHost; smol executors are not used.

When a model makes a structured call to an unselected tool, Pi's own agent loop
returns a `Tool <name> not found` error result to the model and continues. Repeated
unknown calls are bounded by `runtime_options.max_stop_attempts`. Textual DSML
tool-call markup has no Pi tool-call ID and is never executed. The bridge detects
that markup and returns feedback through the same Pi session so the model
can answer or explain the missing capability; repeated invalid output fails the
Run with `output_validation` instead of being reported as a successful answer.

Complete SDK results and first captured query output travel through private,
digest-checked files. Missing captures fail the Application. The public journal
persists the original before acknowledging a tool result; large results have a
bounded display and a ContextRef when context storage is available. Select
`loom_retrieve_context` when the agent needs to inspect those artifacts. Read
offset/limit remain the original query boundary, not a license to reread the file.

For Bash, the original **collected** stdout/stderr is preserved. The pinned SDK does
not expose pipe EOF and may close a quiet pipe inherited by a background process.
Its coverage is therefore `captured_stream`, completeness `unknown`, with an
explicit limitation; exit 0 never establishes complete output or trusted memory
evidence. Timeout, abort and missing exit status are uncertain, not success.
Cancellation cleans managed descendants using the instance's inherited process
token. This is not an OS sandbox; configurations requiring an unsupported sandbox
or protected Shell query mapping are rejected before execution.

## Model projection

| Profile setting | Pi behavior |
| --- | --- |
| `adapter: openai_chat` / `openai_responses` | Native OpenAI Completions / Responses SDK transport, SSE |
| `model` | Preserve the model ID, removing the legacy `openai/` or `gemini/` routing prefix |
| `base_url`, `api_key` | Selected endpoint and in-memory credentials; no user auth discovery |
| Effective request headers | Literal private headers; no Pi command/env interpolation |
| `temperature`, `max_output_tokens` | Native stream options; protocol-appropriate output budget |
| `context_window`, `input_token_limit`, legacy `max_tokens` | Resolved context metadata; `max_output_tokens` is the generation limit |
| `timeout` | Seconds per model attempt, including an already-open SSE stream; cancellation remains immediate |
| `num_retries`, `retry_delay`, `max_retry_delay` | Bounded exponential retry of transient failed no-tool turns; native nested retries disabled |
| `requests_per_minute` | Minimum interval per Pi instance, including retry/Stop continuation attempts |
| `context_cache` | Pi's native short/none cache hint; actual cache support is provider-dependent |
| `extra_body` | Explicit vendor request fields; cannot override model, conversation, tools or mapped generation fields |
| `top_p`, `seed`, `reasoning_effort` | Explicit provider parameters; Responses maps reasoning effort into `reasoning.effort` |
| `tool_choice: auto/none`, `parallel_tool_calls: false` | Compatible no-tool settings; no tool schema is emitted |

For a personal ChatGPT subscription, log in once through Pi and select a Pi-only
profile. AgentLoom passes the path to Pi's own `auth.json` into its isolated
process; Pi reads it and refreshes OAuth credentials. The default location is
`~/.pi/agent/auth.json`, or `PI_CODING_AGENT_DIR/auth.json` when set in the parent
environment. Do not put a token in `llm.yaml`. A missing or expired login fails
the Run; it never opens an interactive login or changes providers.

```yaml
model:
  codex_luna:
    adapter: openai_codex_responses
    model: gpt-6-luna
    context_window: 272000
    max_output_tokens: 16384
    timeout: 300
    num_retries: 0
    reasoning_effort: xhigh
    web_search: auto
```

Set `model_type: codex_luna` on only the Pi Agents that should use the
subscription. `web_search` is `off`, `auto`, or `required`. `required` requests
Pi's native `web_search` on every Model request and fails a turn without a
completed search call. The bridge records the completed call and structured URL
citations in Model evidence and Run events, then appends clickable sources to a
plain-text answer when citations exist. It does not infer citations from answer
text. Luna reasoning defaults to `xhigh`; set `reasoning_effort: max` explicitly
for the higher level. The Codex adapter rejects API keys, custom base URLs, and
custom authorization headers. Pi's native function tools have optional fields;
Codex requests mark those function schemas non-strict while AgentLoom continues
to validate and authorize tool arguments before execution.

Other protocols/settings fail explicitly. `system_prompt_boundary` is unsupported.
Tool forcing/parallel tools are rejected. Provider error bodies are never exposed
in public errors because they may echo credentials or prompts.

## Lifecycle and limits

- Selected native and platform tools, Worker, Goal and compatible same-runtime
  checkpoint resume are enabled. Unselected tools fail immediately. Native
  grep/find/ls and optional professional writes remain unsupported; no implicit
  Markdown tools are injected.
- No implicit local tools, extensions, skills, prompt templates, context files,
  user settings or saved sessions are discovered. A selected Codex profile uses
  Pi's saved OAuth credential file and its configured native web-search mode.
- Stop uses the invocation's AgentLoom Hook Run. A block continues the same native
  in-memory session with the reason/context. `runtime_options.max_stop_attempts`
  bounds terminal-delivery attempts (default 3), including structured-output
  corrections; persistent rejection fails the Application.
- Sequential workflow tasks may continue the current in-memory session.
  Persisted resume restores an SDK 0.87.1 session only for the same Application,
  task, selected definition, bridge/state version and workspace. A resumed
  attempt uses a new Run while preserving the original call identities.
  Older Pi checkpoints are rejected; start a new Task. `additional_args` is
  explicitly unsupported.
- Bridge stdout contains only validated v2 frames. Application/CLI stdout follows
  its existing output contract. Cancellation, EOF, invalid frames and close settle
  pending requests and clean up the owned process group.
- Model retries do not replay completed tools. Committed native and Worker
  results can be reconciled into the restored SDK session without invoking the
  tool again. Calls interrupted before dispatch become explicit not-executed
  results; executing or otherwise uncertain effects stop automatic resume.

## Verification

```sh
.venv/bin/python -m pytest tests/pi_test -q
cd src/runtimes/pi/bridge && npm run typecheck
```

The tests use real Application assembly, registry, SDK and HTTP requests. Only
the remote model service is replaced by deterministic SSE fixtures. Process
failure tests corrupt/terminate actual child processes at the OS boundary.

`tests/pi_test/run_live_applications.py` is an opt-in live campaign. It copies a
private `llm.yaml` into isolated projects with mode 0600, runs 32 Applications
across eight named profiles, and records receipts/answers separately from secrets.
Never commit campaign private configurations or execution logs.
