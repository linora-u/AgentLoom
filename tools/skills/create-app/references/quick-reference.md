# AgentLoom Application Configuration Quick Reference

> This document is for Agents and users to quickly look up information during the configuration process. For full details, refer to the configuration documentation within the project.
>
> **Path convention**: All relative paths are based on the project root directory (i.e., the directory containing `config/llm.yaml`). `config/llm.yaml` only exists at the project root level and never appears in Application-level subdirectories, so there is no ambiguity.
>
> **Custom tools**: Plain Python functions are sufficient — no `@tool` decorator is needed. The framework dynamically imports tools via the YAML `module` + `function` configuration.

---

## 1. All Predefined Tools

| Tool Name | Description | Common Use Cases |
|-----------|-------------|-----------------|
| **File Reading** | | |
| `read_file` | Read file contents (supports offset/limit) | Code analysis, file inspection |
| `get_file_outline` | Get code outline (functions/classes/structs) | Structure analysis |
| **File Writing** ¹ | | |
| `write_file` | Create new file or overwrite existing | Report generation, file creation |
| `write_whole_file` | Write entire file | Large file writing |
| `edit_file` | Edit file (find & replace) | Code modification |
| `write_markdown_file` | Write Markdown file | Documentation/report generation |
| `write_markdown_file_raw` | Write raw Markdown | Unescaped Markdown |
| `append_markdown_sections` | Append Markdown sections | Incremental report generation |
| **File Operations** | | |
| `move_file` | Move a file | File organization |
| `rename_file` | Rename a file | File renaming |
| `copy_file` | Copy a file | File backup |

> ¹ **File History Backup**: When checkpoint is enabled, file-writing tools (`edit_file`, `write_file`, `create_file`, `write_markdown_file`, `move_file`, `copy_file`) automatically create a pre-edit backup via the FileHistoryHook. Backups are stored under `.logs/{agent}/checkpoints/{task_id}/file-history/` and can be used to rewind file state on resume.
| `delete_file` | Delete a file | Clean up temporary files |
| **Directory & Search** | | |
| `browse_directory` | Browse directory structure | Project structure analysis |
| `list_files_glob` | Search files with glob pattern | Batch file location |
| `search_keyword_in_directory` | Search keywords in directory | Keyword location |
| `search_keyword_with_context` | Search keywords with context | Code reference analysis |
| `ripgrep_search_directory` | High-performance ripgrep search | Large repository search |
| `search_files` | Search files | Filename search |
| `code_search` | Search code | Semantic code search |
| `search_and_replace` | Search and replace in files | Batch modifications |
| `code_replace` | Replace code | Code-level replacement |
| `code_edit` | Code edit | Smart code editing |
| `ast_grep_search_file` | AST pattern search | Syntax-level search |
| **Git Operations** | | |
| `get_git_diff_content` | Get Git diff | PR review, change analysis |
| `git_grep_files` | Git grep search | Search within Git repository |
| `git_commit_files` | Git commit specific files | Auto commit |
| `git_auto_commit` | Git auto commit | Auto save |
| `git_check_dirty` | Check uncommitted changes | Status check |
| `is_path_in_repo` | Check if path is in Git repository | Path validation |
| **Shell & Skills** | | |
| `shell_tool` | Execute Shell commands (whitelist restricted) | Build, test |
| `load_skill` | Load a specific skill | On-demand skill loading |
| `list_skills` | List available skills | View available skills |

---

## 2. model_type Selection Rules (Dynamic Discovery)

`model_type` is not a fixed enum — it should be dynamically read from the project's `config/llm.yaml`:

1. Read `model.default_model_type` as the default type.
2. Read the remaining keys under `model` as available types (excluding reserved keys `default_model_type` and non-dict values).
3. In interactive scenarios, confirm with the user: use default / specify explicitly / custom.
4. Custom values must already be defined in `config/llm.yaml`, otherwise a runtime error will occur.

Quickly check available `model_type` values for the current project (run from the project root):

```python
import yaml
from pathlib import Path

cfg = yaml.safe_load(Path("config/llm.yaml").read_text(encoding="utf-8")) or {}
model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
default_type = model_cfg.get("default_model_type")
types = [k for k in model_cfg if k != "default_model_type" and isinstance(model_cfg[k], dict)]
print("default_model_type:", default_type)
print("available_model_types:", types)
```

---

## 3. execution_env.type Options

| Value | Description | Security | Use Cases |
|-------|-------------|----------|-----------|
| `local` | Run directly on local host machine | ⚠️ Low | Development/debugging, trusted environments |
| `docker` | Run in Docker container isolation | ✅ High | Production environments |
| `e2b` | E2B cloud sandbox | ✅ High | SaaS products |
| `wasm` | WebAssembly local sandbox | ✅ High | Lightweight isolation |

---

## 4. tool_call_type Comparison

| Value | Agent Type | Invocation Method | Flexibility | Recommended Scenarios |
|-------|-----------|-------------------|-------------|----------------------|
| `code_act` | `CodeAgentV2` | Write Python code to invoke | High (loops, conditionals, multi-step orchestration) | **Recommended for Supervisor**, complex Workers |
| `tool_call` | `ToolCallingAgentV2` | Structured tool_call messages | Low (single tool call at a time) | Simple Workers |

---

## 5. Common Naming for agent_function_schema.inputs

| Parameter Name | Use Cases | Example Value |
|---------------|-----------|---------------|
| `query` | General task description | `"Analyze the code structure of the src/ directory"` |
| `file_path` | Single file operations | `"src/main.py"` |
| `file_paths` | Multiple file operations | `"src/a.py, src/b.py"` |
| `module_name` | Module-level operations | `"authentication"` |
| `context` | Context passed from upstream Worker | Analysis result text from the previous stage |
| `config` | Configuration information | JSON-formatted parameter set |
| `target_dir` | Directory-level operations | `"src/lib/"` |

---

## 6. System-Level Checkpoint Configuration

AgentLoom provides a **built-in checkpoint/resume/heartbeat system** that every application inherits automatically. Configuration lives in `config/system.yaml` under `checkpoint.*` — no application-level code needed.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `checkpoint.enabled` | `bool` | `true` | Global switch. Set `false` only for short throwaway scripts |
| `checkpoint.cleanup_on_success` | `bool` | `true` | Auto-delete checkpoint dir after success. Use `false` during debugging |
| `checkpoint.max_resume_age` | `int` (sec) | `604800` | 7-day retention window. Expired checkpoints are non-resumable |
| `checkpoint.heartbeat_interval` | `int` (sec) | `5` | Heartbeat write frequency for crash detection |

> **Note**: `checkpoint.*` is a system-level config. It is **not** overridable per Agent YAML (not in the overlay whitelist). To change checkpoint behavior per application, place a `config/system.yaml` in the application directory.

---

## 7. Overlay Whitelist Fields (Agent YAML Can Override System Config)

Only the following 7 top-level fields can override `config/system.yaml`:

| Field | Type | Description |
|-------|------|-------------|
| `system` | `dict` | System metadata |
| `smart_summary` | `any` | Context compression strategy |
| `tool_access_control` | `dict` | Working directory and path filters |
| `execution_env` | `dict` | Execution environment |
| `code_agent` | `dict` | Code execution permissions |
| `tools` | `dict` | Tool configuration (note: this is a dict, different from the Agent's tools list) |
| `prompt` | `str/dict` | System Prompt template path |

---

## 8. Key Constraints Checklist

| # | Constraint | Description |
|---|-----------|-------------|
| 1 | **LLM config isolation** | Agent YAML must not contain `model`/`llm`/`langfuse` — these belong in `config/llm.yaml` only |
| 2 | **worker_agents uses path only** | The `name` field is prohibited |
| 2a | **Shorthand path must include file extension** | e.g. `scan.yaml`, not `scan` |
| 2b | **Supervisor in `workflows/`, Workers in `workflows/worker_agents/`** | Framework validates directories exist |
| 3 | **Tools are plain functions (no decorators), descriptions come from docstrings** | The description in YAML is ignored |
| 4 | **module/function must be paired** | Custom tools must have both configured |
| 5 | **workflow uses `\|` or non-empty `list[str]`** | Single workflows use literal blocks; sequential workflows use list items, each preferably a literal block |
| 6 | **Lists override, not append** | Overriding `default_loaded_tools` requires a complete list |
| 7 | **Worker return values are strings** | `None` → `""`, others → `str()` |
| 8 | **inputs keys must be valid identifiers** | Must satisfy Python `isidentifier()` |

---

## 9. Directory Structure & `worker_agents.path` Resolution Rules (Important)

### Directory Structure (Mandatory)

Agent YAML files **must** follow this layout. The framework validates that the directories exist:

```
applications/{app_name}/
└── workflows/                          ← Supervisor YAML must be here
    ├── {app_name}_agent.yaml
    └── worker_agents/                  ← Worker YAML must be here
        ├── worker_a.yaml
        └── worker_b.yaml
```

### Path Resolution Patterns

`worker_agents.path` is NOT "always resolved relative to the project root" — it follows three resolution patterns:

| Pattern | Example | Resolution Result |
|---------|---------|-------------------|
| Filename with extension (no `/`) | `code_scan.yaml` | Resolved relative to the `worker_agents/` directory adjacent to the current Supervisor YAML |
| Relative path with `/` | `applications/code_review/workflows/worker_agents/code_scan.yaml` | Resolved relative to the project root |
| Absolute path | `/home/user/project/applications/.../code_scan.yaml` | Used as-is |

> **Important**: Shorthand filenames **must include a file extension** (`.yaml`, `.yml`, or `.md`). Bare names like `code_scan` (without extension) will raise an error.

> Best practice: Use the shorthand filename (e.g. `code_scan.yaml`) when workers are directly in `worker_agents/`. Use the full relative path when referencing workers from a different app or from a subdirectory (e.g. `applications/my_app/workflows/worker_agents/analysis/deep_scan.yaml`).
