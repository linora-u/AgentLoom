# Configuration paths and migration

Global configuration paths use the AgentLoom project root. Application configuration and Agent YAML paths use the Application root: the parent of the nearest `workflows/` directory. Nested Workers use that same root.

This applies to file-backed system prompts, Worker references, MCP files, smol prompt templates, Skill roots, Hook Bundles, path include/exclude lists and dangerous Shell paths. `runtime.root_dir` remains global and project-relative. CLI arguments, Python API inputs, tool arguments and execution working directories retain their existing rules.

Inherited paths keep their declaring root. An Application that overrides an MCP timeout still uses the inherited global MCP file. Overriding its path selects a file relative to the Application instead. Wildcards and existing capability access checks are retained.

There is no file-existence fallback. Migrate existing Application configuration:

| Old declaration | New declaration |
| --- | --- |
| `../config/prompts/role.md` or `../../config/prompts/role.md` | `config/prompts/role.md` |
| Worker shorthand `worker.yaml` | `workflows/worker_agents/worker.yaml` |
| `applications/<app>/config/mcp.json` | `config/mcp.json` |
| Cross-Application Worker | `../other_app/workflows/worker_agents/worker.yaml` |
| Project-relative permission directory `secrets` | `../../secrets` (adjust for nested Applications) |

Absolute paths and home expansion retain each capability's existing rules. Relative paths support parent traversal. Validate migrated Applications with `.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/<app>`; this command argument is still relative to the invoking directory.
