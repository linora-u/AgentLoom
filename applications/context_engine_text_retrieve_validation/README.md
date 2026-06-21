# ContextEngine Text Retrieve Validation

Validates the worker-as-tool ContextEngine path for large plain-text output.

Input: a natural-language validation request.

Output: `TEXT_CONTEXT_RETRIEVE_PASS verification_value=TEXT-CTX-7319`.

Supervisor/Worker/Tool split:

- Supervisor calls `text_payload_worker`, receives only a preview plus `ContextRef`, then calls `loom_retrieve_context`.
- Worker calls `make_context_engine_text_payload` and returns the exact large payload.
- Tool deterministically generates the hidden validation record in the middle of the payload.

Run:

```bash
.venv/bin/loom run applications/context_engine_text_retrieve_validation/workflows/context_engine_text_retrieve_validation_agent.yaml "Run the ContextEngine text retrieval validation."
```

Validation:

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/context_engine_text_retrieve_validation
```

Real regression:

```bash
AGENT_LOOM_RUNTIME_ROOT=/tmp/agentloom-context-engine-apps .venv/bin/python tests/agent_test/context_engine/real_context_engine_application_validation.py --case text
```
