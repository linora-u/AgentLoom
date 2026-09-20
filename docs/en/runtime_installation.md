# Runtime installation profiles

AgentLoom requires Python 3.12 or newer. Choose a Python profile explicitly:

- `pi` installs the shared platform without `smolagents` or its instrumentation.
  Pi additionally requires Node.js **22.19 or newer** and npm. Release validation
  and CI use **22.19.0**.
- `smol` adds the pinned **smolagents 1.26.0** runtime and its instrumentation.
  The existing `./install` source installer selects this profile, preserving its
  original default Agent behavior.

The two profiles can coexist. `--all-groups` includes development/build groups;
it does **not** select runtime extras.

## Run from a checkout

For Pi:

```sh
uv sync --locked --no-dev --extra pi
uv run --locked --no-dev --extra pi loom install-runtime pi
uv run --locked --no-dev --extra pi loom run path/to/application.yaml
```

Select `agent_runtime: pi` in YAML. At the ticket 09/13 baseline, disable
checkpoint (`checkpoint: {enabled: false}`), select `read` or `pi_read` for
official reading, and select platform/professional tools explicitly. Writes,
Shell and recovery depend on later runtime tickets.

For existing smol YAML:

```sh
uv sync --locked --extra smol
uv run --locked --extra smol loom run path/to/existing-application.yaml
```

For development and the complete test suite:

```sh
uv sync --locked --all-groups --extra smol
uv run --locked --extra smol loom install-runtime pi
uv run --locked --extra smol pytest tests/
```

## Install the built release outside the checkout

Build tools are pinned in `pyproject.toml`. Export runtime dependencies from the
committed `uv.lock`; hashes are retained. `uv build` builds the wheel from the
sdist, exercising the resources that will actually ship.

```sh
uv build --out-dir /tmp/agentloom-release
uv export --locked --no-dev --extra pi --no-emit-project -o /tmp/agentloom-pi.txt
uv venv --python 3.12 /tmp/agentloom-pi-env
uv pip sync --python /tmp/agentloom-pi-env/bin/python --require-hashes /tmp/agentloom-pi.txt
uv pip install --python /tmp/agentloom-pi-env/bin/python --no-deps /tmp/agentloom-release/agentloom-1.0.1-py3-none-any.whl
cd /path/to/your/application-project
/tmp/agentloom-pi-env/bin/loom install-runtime pi
/tmp/agentloom-pi-env/bin/loom run applications/example/workflows/root.yaml
```

For the smol profile, export `--extra smol` instead and use a separate environment.
The wheel is the same; its declared extras describe the runtime dependencies.
Do not use an editable install to validate a release.

The Python package includes AgentLoom's bridge TypeScript, JSON schema, npm
manifest/lock and professional tool query files. It contains no upstream Pi SDK,
`node_modules`, compiled bridge or local install marker. `loom install-runtime pi`
downloads the fixed SDK **0.79.4** with `npm ci`, builds the bridge inside the
installed `agentloom/adapters/pi/bridge/` directory and validates its assets.
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

The output directory must be new. The verifier builds wheel/sdist, creates two
locked non-editable environments, and runs Application and CLI probes outside
the checkout with isolated Python imports. The model HTTP peer is controlled;
Pi and smol Agent loops and tool execution are real. Reports retain dependency
lists, artifact/lock hashes, Node/Python versions, Run evidence and logs.

Ticket 13 validates the release mechanism at its dependency baseline. Ticket 14
must rebuild and rerun this verification on the final candidate containing the
write/Shell and recovery work from tickets 10/12.
