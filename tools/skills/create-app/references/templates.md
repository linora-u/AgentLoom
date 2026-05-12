# File Generation Templates

> This document contains complete generation templates for all files in Phase 3. Referenced by `SKILL.md`.
>
> **Path convention**: All `<app_name>` placeholders should be replaced with the actual Application name (lowercase + underscores); all relative paths are resolved from the project root directory.

---

## 3.1 Supervisor YAML

**File path**: `applications/<app_name>/workflows/<app_name>_agent.yaml`

```yaml
name: "<app_name>_agent"
description: |
  <User-provided one-line description, expanded into 2-3 sentences of complete role positioning>

# model_type options:
# - Omit only when config/llm.yaml sets model.default_model_type
# - Specify: Use an explicit type (must exist in the model node of config/llm.yaml)
# model_type: "<selected_model_type>"
tool_call_type: "<tool_call_type>"

workflow: |
  # <Application Name> Workflow

  ## Background
  You are a **<professional role>**. The current task is <task objective>.

  ## Execution Flow
  (Note: Place a Mermaid flowchart TD diagram here, with nodes containing each Worker invocation step)

  ## Execution Principles
  1. Strictly call each Worker in the order shown in the flowchart
  2. Each Worker's output serves as the input context for the next Worker
  3. Finally, consolidate all Worker results and output a complete report

tools:
  # Predefined tools
  - name: "read_file"
  - name: "browse_directory"
  # Custom tools (if any)
  # - name: "<tool_name>"
  #   module: "applications.<app_name>.agent_tools.<module>"
  #   function: "<function_name>"

# Worker agents — must be placed in workflows/worker_agents/ directory
# Shorthand: just the filename (e.g. "<worker_name_a>.yaml") when in same app
# Full path: "applications/<app_name>/workflows/worker_agents/<worker_name_a>.yaml" for cross-app references
# Note: Worker YAMLs resolve their whitelisted overrides independently.
# If a Worker needs extra filesystem access, repeat tool_access_control rules in that Worker YAML.
worker_agents:
  - path: "<worker_name_a>.yaml"
  - path: "<worker_name_b>.yaml"
  # ...

# Private Skills (optional, list format recommended)
# skills:
#   - path: "skills/agent-recall-with-files"
#     platform: "Claude"
#     invocation-control:
#       allow-model: "force-inject"
#       allow-hook: true

execution_env:
  type: "<execution_env_type>"
```

Sequential workflow variant:

```yaml
workflow:
  - |
    # First workflow
    <Instructions for the first runtime run>
  - |
    # Second workflow
    <Instructions for the second runtime run; memory from the first run is preserved>
```

---

## 3.2 Worker YAML (One File per Stage)

**File path**: `applications/<app_name>/workflows/worker_agents/step<N>_<name>.yaml`

```yaml
name: "step<N>_<name>"
description: "<stage description>"
tool_call_type: "<tool_call_type>"
# model_type follows the same strategy as Supervisor
# model_type: "<selected_model_type>"
tools:
  - name: "<tool1>"
  # ...

# If this Worker must access directories outside the workspace, declare it here.
# Worker YAML does NOT inherit the Supervisor's external path allowlist.
# tool_access_control:
#   path_validation:
#     - tools: ["read_file", "edit_file", "write_markdown_file", "grep_search", "glob_search", "shell_tool"]
#       include_paths:
#         - "/absolute/path/outside/workspace"

planning_interval: 3    # If enabled
max_steps: 40

# Private Skills (optional)
# skills:
#   - "skills/agent-recall-with-files"

workflow: |
  # Step <N>: <Stage Name>

  ## Background
  You are a **<professional role>**, responsible for <specific responsibilities>.

  ## Core Responsibilities
  1. **<Responsibility 1>**: <Description>
  2. **<Responsibility 2>**: <Description>

  ## Constraints
  - ❌ <Prohibited behavior>
  - ✅ <Recommended practice>

  ## Output Requirements
  - Format: <Format requirements>
  - Must include: <Required content>

agent_function_schema:
  description: |
    <Description of this Worker when called as a tool>
  inputs:
    query:
      description: "<Primary input parameter description>"
      required: true
  output:
    description: "<Output description>"
```

---

## 3.3 Entry Script

**File path**: `applications/<app_name>/<app_name>_app.py`

```python
#!/usr/bin/env python3
"""
<app_name> – Application entry script.

How to run (execute from the project root directory):
    <python> applications/<app_name>/<app_name>_app.py

Or using runner:
    <python> src/runner.py applications/<app_name>/workflows/<app_name>_agent.yaml

Or using AgentLoom CLI (if installed):
    loom run applications/<app_name>/workflows/<app_name>_agent.yaml
"""

import os
import sys

# Ensure the project root directory is in sys.path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.runner import run_app


def main():
    run_app("applications/<app_name>/workflows/<app_name>_agent.yaml")


if __name__ == "__main__":
    main()
```

---

## 3.4 Custom Tools (If Any)

**File path**: `applications/<app_name>/agent_tools/<module_name>.py`

```python
"""
<Tool module description>

Note: Dynamically loaded tools are plain Python functions — no decorators are needed.
The framework dynamically imports them via the module + function configuration in YAML.
Tool descriptions are automatically extracted from the function's docstring; the description field in YAML is ignored.
"""


def <function_name>(<params>) -> str:
    """
    <Detailed description of the tool's functionality — this docstring is extracted by the framework as the tool description.>

    Args:
        <param>: <Parameter description>

    Returns:
        <Return value description>
    """
    # TODO: Implement tool logic
    pass
```

---

## 3.5 App-level config/system.yaml (If Any)

**File path**: `applications/<app_name>/config/system.yaml`

```yaml
# App-level system configuration override
# This file is deep merged on top of the global config/system.yaml

tool_access_control:
  # include_paths: []
  # exclude_paths: []
  # tool_access_control:
  #   - tools: ["read_file", "edit_file"]
  #     exclude_paths: ["build"]

# tools:
#   default:
#     - "read_file"
#     - "shell_tool"
#     - ...
```

---

## 3.6 Custom sysprompt (If Any)

**File path**: `applications/<app_name>/sysprompt/code_agent.yaml`

> Only generated when the user explicitly requests it. In most cases, the framework's default prompt is sufficient.

```yaml
# Custom system prompt template
# The framework loads this file and uses its content as the Agent's system prompt,
# replacing the default built-in prompt.
system_prompt: |
  You are a professional <role description>.

  ## Core Responsibilities
  <Describe the Agent's primary responsibilities>

  ## Behavioral Constraints
  - <Constraint 1>
  - <Constraint 2>

  ## Output Format
  <Describe expected output format>
```

> **Note**: The `prompt` field in Agent YAML must point to this file for it to take effect:
> ```yaml
> prompt:
>   path: "applications/<app_name>/sysprompt/code_agent.yaml"
> ```

---

## 3.7 Single Agent Mode YAML (Replaces 3.1 and 3.2 When User Chooses Single Agent)

**File path**: `applications/<app_name>/workflows/<app_name>_agent.yaml`

```yaml
name: "<app_name>"
description: "<one-line description>"
# model_type options:
# - Omit only when config/llm.yaml sets model.default_model_type
# - Specify: Use an explicit type (must exist in the model node of config/llm.yaml)
# model_type: "<selected_model_type>"
tool_call_type: "<tool_call_type>"
max_steps: <N>

tools:
  - name: "<tool1>"
  - name: "<tool2>"
  # ...

# Private Skills (optional, all three formats are supported)
# skills: "skills/agent-recall-with-files"
# skills:
#   path: "skills/agent-recall-with-files"
#   platform: "Claude"
# skills:
#   - "skills/agent-recall-with-files"
#   - path: "applications/<app_name>/skills/domain-skill"
#     invocation-control:
#       allow-model: true
#       allow-hook: true

workflow: |
  # <Application Name>

  ## Background
  You are a **<professional role>**, responsible for <specific responsibilities>.

  ## Tasks
  1. <Step 1>
  2. <Step 2>
  3. <Step 3>

  ## Constraints
  - ❌ <Prohibited behavior>
  - ✅ <Recommended practice>

  ## Output Requirements
  - Format: <Format requirements>
  - Must include: <Required content>

# Single Agent mode notes:
#   - No worker_agents field needed (no sub-Agents)
#   - No agent_function_schema field needed (won't be called by other Agents)
#   - Run directly via src/runner.py
```

> **Full single Agent example**: See the `simple_scanner` example at the end of `references/full-example.md`.

---

## 3.8 skills Field Reference (Applies to All Agent YAMLs)

```yaml
# Format 1: String (simplest)
skills: "skills/agent-recall-with-files"

# Format 2: Dictionary (single Skill)
skills:
  path: "skills/agent-recall-with-files"
  platform: "Claude"
  invocation-control:
    allow-model: "force-inject"
    allow-hook: true

# Format 3: List (recommended)
skills:
  - "skills/agent-recall-with-files"
  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false
      allow-hook: true
```
