# Troubleshooting Guide

> Common errors and solutions for AgentLoom Application configuration and runtime.

---

## 1. Worker Loading Failure

### Symptoms
```
Error: Failed to load worker agent: ...
```

### Troubleshooting Steps

| Check Item | Description |
|-----------|-------------|
| `worker_agents` used `name` instead of `path` | ❌ `name: code_scan` → ✅ `path: "applications/.../code_scan.yaml"` |
| `path` has a typo | Confirm the file actually exists, pay attention to case sensitivity |
| `path` resolution rule misunderstood | Filename only (no `/`) resolves from the `worker_agents/` directory; relative path with `/` resolves from the project root |
| Worker YAML syntax error | Run `python -c "import yaml; yaml.safe_load(open('path/to/worker.yaml'))"` to check |
| Worker missing `agent_function_schema` | Worker must define this field to be callable as a tool by the Supervisor |

> Additional note: `path` must point to a file, not a directory. The validation script checks `exists + is_file`.

### Quick Verification

Run from the project root directory:

```python
import yaml
with open('applications/<app_name>/workflows/worker_agents/<worker_name>.yaml') as f:
    cfg = yaml.safe_load(f)
    print('name:', cfg.get('name'))
    print('schema:', 'agent_function_schema' in cfg)
```

---

## 2. Custom Tool Import Failure

### Symptoms
```
ModuleNotFoundError: No module named 'applications.xxx.agent_tools.yyy'
ImportError: cannot import name 'zzz' from ...
```

### Troubleshooting Steps

| Check Item | Description |
|-----------|-------------|
| Are `module` and `function` paired? | Both must appear together in the YAML |
| `module` path uses dot notation | ✅ `applications.code_review.agent_tools.repo_context` |
| Does the module file exist? | Verify the corresponding `.py` file is actually on disk |
| Does the function name match? | The `function` in YAML must exactly match the function name in the `.py` file |
| Are decorators used? | ❌ `@tool` decorator, ✅ Plain function |
| Missing `__init__.py` | The `agent_tools/` directory usually doesn't need `__init__.py` — the framework imports dynamically |
| Current working directory | Must run from the project root directory |

### Quick Verification

Run from the project root directory:

```python
from applications.<app_name>.agent_tools.<module> import <function_name>
print(type(<function_name>))
print(<function_name>.__doc__[:100])
```

---

## 3. model_type Does Not Exist

### Symptoms
```
KeyError: 'xxx' not found in llm config
ValueError: Unknown model_type: ...
```

### Troubleshooting Steps

1. Check whether the model_type is defined in `config/llm.yaml`
2. Dynamically confirm available types from the `model` node in `config/llm.yaml` (excluding `default_model_type` and non-dict values)
3. Custom model_type values must first be added as a configuration block in `config/llm.yaml`

### Quick Verification

Run from the project root directory:

```python
import yaml
with open('config/llm.yaml') as f:
    cfg = yaml.safe_load(f)
model_cfg = cfg.get('model', {}) if isinstance(cfg, dict) else {}
available = [k for k in model_cfg if k != 'default_model_type' and isinstance(model_cfg[k], dict)]
print('default_model_type:', model_cfg.get('default_model_type'))
print('Available model_types:', available)
```

---

## 4. workflow Field Formatting Lost

### Symptoms
Workflow content displays as a single line, with all Markdown formatting lost.

### Cause
Single-run `workflow` did not use the YAML `|` multi-line text block syntax, or a sequential `workflow` list has items that are not written as text blocks.

### Fix
```yaml
# ❌ Wrong
workflow: "# Title\n## Steps\n1. xxx"

# ❌ Wrong
workflow:
  # Title

# ✅ Correct: single workflow
workflow: |
  # Title

  ## Steps
  1. xxx

# ✅ Correct: sequential workflows
workflow:
  - |
    # First workflow
    ...
  - |
    # Second workflow
    ...
```

---

## 5. LLM Configuration Mistakenly Written in Agent YAML

### Symptoms
```
Warning: Field 'model'/'llm'/'langfuse' found in agent YAML, will be ignored.
```

### Cause
Agent YAML should not contain `model`, `llm`, `langfuse`, or similar fields. These configurations are managed centrally in `config/llm.yaml`.

### Fix
Remove these fields from the Agent YAML and use `model_type` to reference model configurations defined in `llm.yaml`.

---

## 6. Entry Script Runtime Error

### Symptoms
```
ModuleNotFoundError: No module named 'src'
FileNotFoundError: ... agent.yaml not found
```

### Troubleshooting Steps

| Check Item | Description |
|-----------|-------------|
| Current directory | Must run from the project root directory |
| YAML path | The path in `run_app(...)` in `_app.py` must match the actual file path |
| sys.path | Verify the `project_root` calculation in the entry script is correct (directory hierarchy) |

---

## 7. Validation Script Error

### Symptoms
```
config/llm.yaml not found, unable to locate project root directory
```

### Fix

Run from the project root directory (the script automatically searches upward for `config/llm.yaml` to locate the project root):

```bash
cd <project_root>
<python> <skill_root>/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>
```

---

## 8. Illegal agent_function_schema.inputs Parameter Name

### Symptoms
Validation script error: `Input parameter name 'xxx' is not a valid Python identifier`

### Cause
Keys under `inputs` must be valid Python identifiers (start with a letter or underscore, contain only letters, digits, and underscores).

### Common Mistakes
```yaml
# ❌ Contains hyphen
inputs:
  file-path:
    description: "..."

# ❌ Starts with a digit
inputs:
  1st_query:
    description: "..."

# ✅ Correct
inputs:
  file_path:
    description: "..."
  query:
    description: "..."
```

---

## 9. Skills Loading Failure

### Symptoms
```
Warning: Skill directory not found: skills/xxx
Warning: Duplicate skill 'xxx' loaded, overwriting previous
```
Or the Agent does not exhibit expected Skill behavior at runtime.

### Troubleshooting Steps

| Check Item | Description |
|-----------|-------------|
| `skills` path is incorrect | Paths are resolved relative to the project root (`C.agent_root`). Confirm the directory exists |
| `SKILL.md` file missing | Each Skill directory must contain `SKILL.md` (or `skill.md`) to be recognized |
| `invocation-control.allow-model` value | Only `true`, `false`, or `"force-inject"` are valid. Any other value is ignored |
| Duplicate Skill names | If the same Skill name appears in multiple layers (system / directory / agent YAML), the last one loaded wins (with a warning) |
| `skills` format error | Must be one of: string, dict, or list. A plain list of strings is valid; mixing strings and dicts in a list is also valid |
| Skill expects hooks but hooks are not triggered | Verify `invocation-control.allow-hook: true` is set. Also confirm the hook script path is correct relative to the Skill directory |

### Three-Layer Loading Order

```
Layer 1: config/system.yaml global skills
Layer 2: <project_root>/skills/ directory auto-discovery
Layer 3: Agent YAML skills field
```

Layers are **additive** (not overriding). Same-name Skills loaded later overwrite earlier ones.

### Quick Verification

Check if a Skill directory is valid:

```bash
# Confirm the Skill directory contains SKILL.md
ls <project_root>/skills/<skill_name>/SKILL.md

# Check the YAML frontmatter
head -10 <project_root>/skills/<skill_name>/SKILL.md
```
