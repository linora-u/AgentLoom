# ContextEngine JSON Retrieve Validation

Validates the worker-as-tool ContextEngine path for large JSON output.

Input: a natural-language validation request.

Output: `JSON_CONTEXT_RETRIEVE_PASS verification_value=JSON-CTX-4927`.

Supervisor/Worker/Tool split:

- Supervisor calls `json_payload_worker`, receives only a preview plus `ContextRef`, then calls `loom_retrieve_context`.
- Worker calls `make_context_engine_json_payload` and returns the exact large JSON payload.
- Tool deterministically generates the hidden validation item in the middle of the JSON list.

Run:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-context-engine-json \
  .venv/bin/loom run applications/context_engine_json_retrieve_validation/workflows/context_engine_json_retrieve_validation_agent.yaml
```

Validation:

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/context_engine_json_retrieve_validation
```

Real regression:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-context-engine-apps \
  .venv/bin/python tests/agent_test/context_engine/real_context_engine_application_validation.py --case json
```
