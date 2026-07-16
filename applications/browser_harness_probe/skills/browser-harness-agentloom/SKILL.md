---
name: browser-harness-agentloom
description: "Use when working on AgentLoom browser-harness integration or debugging applications/browser_harness_probe: creating or updating the probe Application, installing the external browser-harness CLI, validating isolated or real Chrome browser control, and diagnosing browser-harness doctor, daemon, Chrome remote debugging, config/llm.yaml, or AgentLoom tool-registration issues."
---

# Browser Harness AgentLoom

## Core Rule

Keep browser-harness as an external editable CLI. Do not add it to AgentLoom `pyproject.toml` or `uv.lock`.

Use this Application as the local proof path:

```bash
cd /Users/bytedance/code/data_clear/AgentLoom-main-skill
.venv/bin/loom run applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml
```

`browser_harness_probe_app.py` is optional. Use it only when you need a custom natural-language request, a `file_logging` override, or `resume`; direct YAML execution is the shorter default path.

## Setup

If `browser-harness` is missing:

```bash
git clone https://github.com/browser-use/browser-harness /Users/bytedance/code/browser-harness
uv tool install -e /Users/bytedance/code/browser-harness
command -v browser-harness
browser-harness --doctor
```

If `/Users/bytedance/code/browser-harness` already exists, do not reclone. Reuse it and run `uv tool install -e /Users/bytedance/code/browser-harness`.

## AgentLoom Environment

- Work in the clean worktree that contains this Application; do not modify an unrelated dirty checkout.
- A clean worktree may not contain ignored `config/llm.yaml`. If AgentLoom raises `Model type 'powerful' is not defined`, copy the local ignored file from the main checkout:

```bash
cp /Users/bytedance/code/data_clear/AgentLoom/config/llm.yaml \
   /Users/bytedance/code/data_clear/AgentLoom-config-env/config/llm.yaml
```

- Keep `applications/browser_harness_probe/config/system.yaml` with `skills: []` so unrelated global Skills do not interfere with this probe.

## Validation Order

Run deterministic checks before model-backed Application runs:

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/browser_harness_probe

.venv/bin/python -c "
import sys
sys.path.insert(0, 'agentloom-framework-skill')
from scripts.scan_tools import scan_app_structure
print(scan_app_structure('applications/browser_harness_probe'))
"

.venv/bin/python -m py_compile applications/browser_harness_probe/browser_harness_probe_app.py
find applications/browser_harness_probe/agent_tools -name '*.py' -print0 |
  xargs -0 .venv/bin/python -m py_compile

.venv/bin/loom create applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml \
  -o /tmp/browser_harness_probe_generated_app.py
.venv/bin/python -m py_compile /tmp/browser_harness_probe_generated_app.py
```

Then run browser probes:

```bash
# Tool-only isolated smoke test, no LLM required.
.venv/bin/python - <<'PY'
from applications.browser_harness_probe.agent_tools.browser_harness_tools import run_demo_probe
print(run_demo_probe(browser_mode="isolated", timeout_seconds="120"))
PY

# AgentLoom isolated path, verifies YAML dynamic tool registration.
.venv/bin/loom run applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml
```

Use the Python wrapper only for a custom mode-specific request:

```bash
.venv/bin/python applications/browser_harness_probe/browser_harness_probe_app.py \
  "Run browser-harness doctor and verify real Chrome only."
```

## Failure Diagnosis

- `browser-harness --doctor` returns nonzero with `chrome running ok`, `daemon alive FAIL`, and `active browser connections FAIL`: not necessarily blocking. Isolated mode starts an ephemeral daemon through `BU_CDP_URL`.
- `profile-use installed FAIL` and `BROWSER_USE_API_KEY set FAIL`: optional. They matter only for cloud browsers or profile sync.
- Isolated mode fails before browser-harness runs: check Chrome path. The Tool uses `BH_CHROME_PATH`, `CHROME_PATH`, then `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
- Isolated mode reaches the target URL and returns `page_info`: the browser-harness CLI and AgentLoom Tool path work.
- Real Chrome fails with `DevToolsActivePort not found` or text asking for `chrome://inspect/#remote-debugging`: browser authorization/setup issue, not AgentLoom registration failure. Ask the user to open `chrome://inspect/#remote-debugging`, tick "Allow remote debugging for this browser instance", click Allow, then rerun.
- AgentLoom loads `browser_harness_doctor`, `run_isolated_demo_probe`, and `run_real_demo_probe` successfully but real Chrome fails: integration works; only browser authorization remains.

## Current Known Result

As of this probe:

- Isolated Chrome passed through direct Tool call and through AgentLoom Application execution.
- Real Chrome reached `run_real_demo_probe` but failed because Chrome remote debugging was not authorized.
- `browser-harness` is installed at `/Users/bytedance/.local/bin/browser-harness` from editable repo `/Users/bytedance/code/browser-harness`.
