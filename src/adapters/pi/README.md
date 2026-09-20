# Pi runtime (ticket 07)

This adapter runs real `@earendil-works/pi-coding-agent` **0.79.4** AgentSessions.
Python owns Application identity, receipts, Hook Run/Stop and resource cleanup;
Pi owns its in-memory conversation and native provider protocol. No smol model,
Tool, Todo, final_answer implementation or message format is required.

## Install and run

Node **22.19+** with npm is required. Use uv as the Python environment and
installation entry point:

```sh
uv run --locked loom install-runtime pi
```

This downloads the published SDK using `npm ci --ignore-scripts`, then builds
AgentLoom's bridge and verifies its imports. `package.json` fixes the SDK at
**0.79.4**; `package-lock.json` fixes transitive versions and integrity hashes.
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
The equivalent Python entry point is `python -m agentloom.adapters.pi.install`.

Use the existing `config/llm.yaml`, then select Pi in an Application workflow:

```yaml
name: direct_answer
agent_runtime: pi
description: Answer the user's question directly.
workflow: Give a concise answer.
tools: []
toolsets: []
```

Disable checkpoint and Goal for this Application. Run with the existing
`loom run applications/<app>/workflows/root.yaml` or `execute_app` entry.
Dependency installation is explicit; Application execution never runs npm.
Python packages include our bridge sources, schema and lock, not `node_modules`
or build artifacts. The same installer prepares the package's Pi directory.
Pi-only dependency profiles and final clean-install acceptance remain ticket 13.

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

Other protocols/settings fail explicitly. `system_prompt_boundary` is unsupported.
Tool forcing/parallel tools are rejected. Provider error bodies are never exposed
in public errors because they may echo credentials or prompts.

## Lifecycle and limits

- Tools, Worker, Goal and checkpoint/resume are **not enabled**. A model returning
  an unselected tool call fails immediately, even though no tools can execute.
- No implicit built-in tools, extensions, skills, prompt templates, context files,
  user settings, saved sessions or environment credentials are discovered.
- Stop uses the invocation's AgentLoom Hook Run. A block continues the same native
  in-memory session with the reason/context. `runtime_options.max_stop_attempts`
  bounds attempts (default 3); persistent rejection fails the Application.
- Sequential workflow tasks may continue the current in-memory session. This is
  not persisted resume. `additional_args` is explicitly unsupported in this stage.
- Bridge stdout contains only validated v1 frames. Application/CLI stdout follows
  its existing output contract. Cancellation, EOF, invalid frames and close settle
  pending requests and clean up the owned process group.
- Bidirectional tool requests are recognized and explicitly rejected until 09.
  09 adds callbacks and tool governance; it must revisit retry semantics before
  permitting any tool side effects to be replayed.

## Verification

```sh
.venv/bin/python -m pytest tests/pi_test -q
cd src/adapters/pi/bridge && npm run typecheck
```

The tests use real Application assembly, registry, SDK and HTTP requests. Only
the remote model service is replaced by deterministic SSE fixtures. Process
failure tests corrupt/terminate actual child processes at the OS boundary.

`tests/pi_test/run_live_applications.py` is an opt-in live campaign. It copies a
private `llm.yaml` into isolated projects with mode 0600, runs 32 Applications
across eight named profiles, and records receipts/answers separately from secrets.
Never commit campaign private configurations or execution logs.
