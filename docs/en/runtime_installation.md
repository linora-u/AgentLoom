# Runtime installation profiles

AgentLoom requires Python 3.12 or newer. Checkout environments use the managed
Python version pinned in `.python-version` (currently 3.14.8). `./install`
downloads it through uv and upgrades an existing `.venv` to that version.
An explicit `UV_PYTHON` overrides this selection. Choose a runtime profile:

- `pi` installs the shared platform without `smolagents` or its instrumentation.
  Pi additionally requires Node.js **22.19 or newer** and npm. Environment proxy
  mode requires **22.21+ on the 22.x line, or 24+**. Release validation and CI use
  **22.21.0**.
- `smol` adds the pinned **smolagents 1.26.0** runtime and its instrumentation.
  The existing `./install` source installer selects `smol` plus `code`, preserving
  its original default Agent and professional-tool environment.
- `code` adds optional AST, outline and LSP dependencies, independently of the
  Agent runtime. Pi without this extra does not install Serena, AST-Grep or the
  Go/Node tool runtimes. Shared Shell governance keeps tree-sitter/Bash in core.
- `durable` adds the pinned DBOS 3.2.0 execution engine and the local executor
  lock. Add `--extra durable` to both `uv sync` and subsequent `uv run` commands
  when using persistent Worker queues. Local execution supports macOS and Linux
  with SQLite; it does not require a system service or a separate database.

The profiles can coexist. `--all-groups` includes development/build groups;
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
source installer uses the checkout's `.venv` by default, with stable launchers
and installation records in `.agentloom`. Framework updates preserve other
packages installed in that shared project environment. `AGENTLOOM_INSTALL_DIR` selects a
custom root with its Python environment in `<root>/venv`. Workspace applications
are installed separately with `uv sync --all-packages`; after changing from the
installer's non-editable framework to an editable checkout, install Pi again
with `.venv/bin/loom runtime install pi`. The installed SDK and compiled bridge
live at `<python-prefix>/share/pi/`; the shipped bridge source
remains separate from the installed SDK and compiled assets.

When selecting professional code tools in YAML, add `--extra code` to **both**
`uv sync` and subsequent `uv run` commands; otherwise uv removes unused extras:

```sh
uv sync --locked --no-dev --extra pi --extra code
uv run --locked --no-dev --extra pi --extra code loom runtime install pi
uv run --locked --no-dev --extra pi --extra code loom run path/to/application.yaml
```

Select `agent_runtime: pi` in YAML and select platform/professional tools
explicitly. Tool permissions and checkpoint policy remain Application settings.

For existing smol YAML:

```sh
uv sync --locked --extra smol --extra code
uv run --locked --extra smol --extra code loom run path/to/existing-application.yaml
```

For development and the complete test suite:

```sh
uv sync --locked --all-groups --extra smol --extra code --extra durable
uv run --locked --extra smol --extra code --extra durable loom runtime install pi
cp config/model.example.yaml config/model.yaml  # Only when no local config exists.
uv run --locked --extra smol --extra code --extra durable pytest tests/
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
downloads the fixed SDK **1.0.0** with `npm ci`, builds the bridge under
`<python-prefix>/share/pi/` and validates its assets.
The environment must be writable during this explicit installation step.

Source, schema or npm lock changes invalidate the installation fingerprint.
A missing or stale installation produces an actionable error before the SDK
starts. Application execution never runs npm or downloads dependencies.

## Managed Worker execution

Ordinary Agent calls and finite `app.worker(path).map(items)` calls manage native
Pi, MCP and tool resources by default, including when concurrency is one.
Factories and trusted Python dependencies remain in the caller. Each map keeps
its own concurrency limit; persistence is explicit through `app.durable()`.

The resource owner uses the installed Python and psutil. It references OpenClaw
source mechanisms and does not install, execute or package OpenClaw. The former
`loom runtime install execution-owner` command returns a diagnostic error.
No owner/plugin selection or launchd/systemd configuration is required.

For durable execution, `execution.resource_status(workflow_id)` reports physical
cleanup and required exit-receipt persistence separately. `execution.storage_status()`
reports safe database metadata and the latest persistence error. A cancelled
workflow can still own resources; use `execution.recover()` with the consumer
stopped before resuming an attempt whose cleanup remains unconfirmed.

Unix SQLite 3.51.1 has a confirmed upstream WAL connection-close deadlock.
Persistent queue initialization rejects this library before accepting work.
Use a verified environment with the upstream fix (SQLite 3.51.2 or another
verified version). Python 3.12 remains supported; the check concerns SQLite.
See [the reproduction and upstream fix](../research/sqlite-wal-close-deadlock-20261014.md).

## Reproduce the release acceptance

With Node 22.21.0 on PATH, run:

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
