# ContextEngine Multi-Worker Validation

Validates that multiple worker-as-tool outputs produce independent `ContextRef` values and remain retrievable by the supervisor.

Input: a natural-language validation request.

Output: `MULTI_CONTEXT_RETRIEVE_PASS log_value=LOG-CTX-8842 search_value=SEARCH-CTX-6194`.

Supervisor/Worker/Tool split:

- Supervisor calls `log_payload_worker` and `search_payload_worker`, then retrieves each original payload by ref.
- Workers call deterministic payload tools and return exact large outputs.
- Tools generate log-like and grep-like payloads with hidden validation records.

Run:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-context-engine-multi \
  .venv/bin/loom run applications/context_engine_multi_worker_validation/workflows/context_engine_multi_worker_validation_agent.yaml
```

Validation:

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/context_engine_multi_worker_validation
```

Real regression:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-context-engine-apps \
  .venv/bin/python tests/agent_test/context_engine/real_context_engine_application_validation.py --case multi
```
