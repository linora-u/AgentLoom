# Skills

AgentLoom Skills are instruction packages discovered from conventional directories:

```text
skills/<skill-name>/SKILL.md
applications/<application>/skills/<skill-name>/SKILL.md
```

An Application definition can add local discovery roots. Paths are relative to
the project root in `config/system.yaml`, and relative to the Application root
in Application or Agent configuration:

```yaml
skills:
  paths:
    - shared/skills
```

`paths` is the only Skill configuration field. It does not select a loading
mode or grant execution privileges.

## Runtime semantics

Skill loading is always model-context-on-demand:

1. Shared definition preflight discovers and parses `SKILL.md` packages for the
   Supervisor and every referenced Worker before allocating a Run.
2. The system prompt receives only each permitted Skill's `name` and `description`.
3. When a task matches, the model calls `skill(name)`.
4. That tool result adds only the selected instructions, base directory, and a
   sampled file list to the conversation.

The catalogue is hidden when the Agent does not have the `skill` tool. There is
no eager mode. Skill activation does not grant file, shell, script, or network
access; the Agent's normal tools and permissions remain authoritative. Use
those normal tools to read package resources or run commands.

Studio and execution use the same static inspection result. Invalid frontmatter,
names, and duplicate names within one scope reject the definition before Run
allocation. Inspection reads Skill data without constructing models, loading tool
implementations, connecting MCP servers, or executing Hooks.

Each prepared Agent definition retains its parsed Skill catalogue and instruction
text. A fresh inspection or invocation sees file edits; an existing invocation
keeps its original instructions. Activation still samples resource file locations
from the current directory; it does not freeze all resource files.

## `SKILL.md` contract

Required frontmatter:

```yaml
---
name: test-driven-development
description: Use when implementing behavior with tests.
---
```

Supported optional frontmatter follows the OpenCode package surface:

```yaml
license: MIT
compatibility: Requires git.
metadata:
  owner: platform
```

Unknown fields are ignored. `hooks` and `enable-hooks` are rejected because
Hooks are an independent execution-authority boundary; configure them through
[`hooks`](hooks.md).

Names must be lowercase kebab-case and at most 64 characters. Descriptions must
be non-empty and at most 1024 characters. Invalid YAML, missing required fields,
and duplicate names in one scope are errors. Agent definitions override
Application definitions, which override project definitions with the same name.

Directories named `generated` are excluded from runtime discovery because
self-learning proposals remain inactive until explicitly promoted.
