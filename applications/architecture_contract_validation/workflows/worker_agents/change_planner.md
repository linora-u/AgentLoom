# Change planner

```yaml
name: change_planner
agent_runtime: smolagents
description: Architecture contract change planner
model_type: powerful
max_steps: 24
todo:
  mode: 'off'
toolsets: []
context_engine:
  min_chars: 80000
  preview_max_chars: 80000
agent_function_schema:
  description: change planner
  inputs:
    query:
      description: JSON result from the previous stage, with absolute workspace and
        unique case_nonce.
      required: true
  output:
    description: Structured JSON stage evidence, preserving workspace and case_nonce.
tools:
- name: read_workspace_file
  module: applications.architecture_contract_validation.agent_tools.workspace_tools
  function: read_workspace_file
worker_agents: []
```

Parse query as the actual repository_investigator result. Independently read CONTRACT.md and at least one implicated source file using read_workspace_file. Produce a concrete repair and regression plan grounded in those findings. Do not edit. Return JSON containing workspace, case_nonce, findings, repair_plan, and required_cases. Required regression identifiers: threshold_equal, discount_before_shipping, override_precedence, invalid_quantity, invalid_price, invalid_config, empty_cart, zero_price, multiple_lines. For zero_price, plan separate empty and nonempty zero-valued cart assertions under positive and zero free-shipping thresholds, including overrides; subtotal zero does not mean empty. Tell implementer to author code and tests, run run_workspace_tests, and preserve existing tests and configs. Preserve input nonce and paths.
