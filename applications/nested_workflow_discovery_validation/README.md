# Nested workflow discovery validation

This Application keeps its only Supervisor below `workflows/groups/review/` and
its Worker below that Supervisor's `worker_agents/` directory. It verifies that
the framework Skill validator and structure scanner discover the same nested
definitions that the Studio domain and runtime use.

Run deterministic validation from the repository root:

```sh
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/nested_workflow_discovery_validation

.venv/bin/python -m agentloom.tui_bridge.domain_cli \
  --project "$PWD" application.validate \
  '{"application_id":"nested_workflow_discovery_validation"}'
```

The scanner is a Skill script rather than an installed module. Use this exact
command to invoke it:

```sh
.venv/bin/python -c "
import importlib.util
from pathlib import Path
path = Path('agentloom-framework-skill/scripts/scan_tools.py')
spec = importlib.util.spec_from_file_location('agentloom_scan_tools', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.scan_app_structure('applications/nested_workflow_discovery_validation'))
"
```

Run the real Application with an isolated runtime home:

```sh
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-nested-workflow-discovery \
  .venv/bin/loom run \
  applications/nested_workflow_discovery_validation/workflows/groups/review/supervisor.yaml \
  --output-format jsonl
```

Success requires two discovered definitions, one completed Worker call returning
`ISSUE_69_NESTED_WORKER_PASS`, a completed Run manifest, and the exact final
result:

```text
ISSUE_69_NESTED_APPLICATION_PASS worker=ISSUE_69_NESTED_WORKER_PASS
```
