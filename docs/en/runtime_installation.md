# Runtime installation profiles

AgentLoom requires Python 3.12 or newer. Choose a Python profile explicitly:

- `pi` installs the shared platform without `smolagents` or its instrumentation.
  Pi additionally requires Node.js **22.19 or newer** and npm. Release validation
  and CI use **22.19.0**.
- `smol` adds the pinned **smolagents 1.26.0** runtime and its instrumentation.
  The existing `./install` source installer selects `smol` plus `code`, preserving
  its original default Agent and professional-tool environment.
- `code` adds optional AST, outline and LSP dependencies, independently of the
  Agent runtime. Pi without this extra does not install Serena, AST-Grep or the
  Go/Node tool runtimes. Shared Shell governance keeps tree-sitter/Bash in core.

The two profiles can coexist. `--all-groups` includes development/build groups;
it does **not** select runtime extras.

## Run from a checkout

For Pi:

```sh
uv sync --locked --no-dev --extra pi
uv run --locked --no-dev --extra pi loom runtime install pi
uv run --locked --no-dev --extra pi loom run path/to/application.yaml
```

The SDK is an npm dependency, not a Python package. `uv` installs AgentLoom and
runs its installer; the installer uses `npm ci` and the committed npm lock to
download Pi. No upstream SDK source needs to be copied into this repository.
For the complete source installation, `./install --runtime pi` performs both
steps automatically. `agentloom update` preserves this runtime choice. The
download lives under `runtimes/pi/bridge/node_modules/` inside the installed
package; editable development uses `src/runtimes/pi/bridge/node_modules/`.

When selecting professional code tools in YAML, add `--extra code` to **both**
`uv sync` and subsequent `uv run` commands; otherwise uv removes unused extras:

```sh
uv sync --locked --no-dev --extra pi --extra code
uv run --locked --no-dev --extra pi --extra code loom runtime install pi
uv run --locked --no-dev --extra pi --extra code loom run path/to/application.yaml
```

Select `agent_runtime: pi` in YAML. At the ticket 09/13 baseline, disable
checkpoint (`checkpoint: {enabled: false}`), select `read` or `pi_read` for
official reading, and select platform/professional tools explicitly. Writes,
Shell and recovery depend on later runtime tickets.

For existing smol YAML:

```sh
uv sync --locked --extra smol --extra code
uv run --locked --extra smol --extra code loom run path/to/existing-application.yaml
```

For development and the complete test suite:

```sh
uv sync --locked --all-groups --extra smol --extra code
uv run --locked --extra smol --extra code loom runtime install pi
cp config/llm.example.yaml config/llm.yaml  # Only when no local config exists.
uv run --locked --extra smol --extra code pytest tests/
```

## Install the built release outside the checkout

Export build and runtime dependencies from the committed `uv.lock`; hashes are
retained, including transitive build dependencies. `uv build` builds the wheel from the
sdist, exercising the resources that will actually ship.

```sh
uv export --locked --only-group build --no-emit-project -o /tmp/agentloom-build.txt
uv build --build-constraints /tmp/agentloom-build.txt --require-hashes --out-dir /tmp/agentloom-release
uv export --locked --no-dev --extra pi --no-emit-project -o /tmp/agentloom-pi.txt
uv venv --python 3.12 /tmp/agentloom-pi-env
uv pip sync --python /tmp/agentloom-pi-env/bin/python --require-hashes /tmp/agentloom-pi.txt
uv pip install --python /tmp/agentloom-pi-env/bin/python --no-deps /tmp/agentloom-release/agentloom-1.0.1-py3-none-any.whl
cd /path/to/your/application-project
/tmp/agentloom-pi-env/bin/loom runtime install pi
/tmp/agentloom-pi-env/bin/loom run applications/example/workflows/root.yaml
```

For the original smol installation, export `--extra smol --extra code` instead
and use a separate environment. For Pi with professional tools, export
`--extra pi --extra code`.
The wheel is the same; its declared extras describe the runtime dependencies.
Do not use an editable install to validate a release.

The Python package includes AgentLoom's bridge TypeScript, JSON schema, npm
manifest/lock and professional tool query files. It contains no upstream Pi SDK,
`node_modules`, compiled bridge or local install marker. `loom runtime install pi`
downloads the fixed SDK **0.87.1** with `npm ci`, builds the bridge inside the
installed `agentloom/runtimes/pi/bridge/` directory and validates its assets.
The environment must be writable during this explicit installation step.

Source, schema or npm lock changes invalidate the installation fingerprint.
A missing or stale installation produces an actionable error before the SDK
starts. Application execution never runs npm or downloads dependencies.

## Reproduce the release acceptance

With Node 22.19.0 on PATH, run:

```sh
uv run --no-project --python 3.12 tests/packaging/validate_profiles.py \
  --output /tmp/agentloom-profile-acceptance --node "$(command -v node)"
```

The output directory must be new. The verifier builds wheel/sdist, creates three
locked non-editable environments, and runs Application and CLI probes outside
the checkout with isolated Python imports. The model HTTP peer is controlled;
Pi and smol Agent loops and tool execution are real. Reports retain dependency
lists, artifact/lock hashes, Node/Python versions, Run evidence and logs. The
environments cover Pi without professional dependencies, Pi with `code`, and
smol with `code`. The smol profile also starts an unchanged existing repository
YAML and its original default toolsets. The LSP tool case exercises its packaged
tree-sitter fallback, not a live language server.

Ticket 13 validates the release mechanism at its dependency baseline. Ticket 14
must rebuild and rerun this verification on the final candidate containing the
write/Shell and recovery work from tickets 10/12.
