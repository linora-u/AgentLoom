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

Shared definition preflight discovers and parses the Supervisor and all referenced
Workers' `SKILL.md` files before allocating a Run. Same-name precedence is
Agent > Application > project, including directories added by `skills.paths`.

Pi Agents register the resolved names, descriptions and entrypoint locations in
Pi's native Skills system. User and project `.pi/skills` discovery stays disabled.
With native `read` or `bash` selected, Pi adds names, descriptions and locations to
`<available_skills>` in its system prompt. The model reads `SKILL.md` on demand and
resolves relative references against the skill directory. For example:

```yaml
agent_runtime: pi
skills:
  paths:
    - skills/news-catalyst
tools:
  - name: read
toolsets: []
```

Registration does not mean the body has been read. Native reads appear in normal
tool records. Registered metadata uses the definition snapshot; native `read`
reads the current file contents for both instructions and references.

Other runtimes continue to use the platform `skill(name)` tool. Their system
prompt contains names and descriptions; activation returns the selected parsed
instructions, base directory and sampled file list. Pi still supports explicitly
selected platform `skill` tools, but does not duplicate the platform summary when
native skill reading tools are available. Without `read`/`bash` or platform `skill`,
the catalogue is not shown. There is no eager mode. Skills grant no additional
file, shell, script or network permissions; existing tool policies still apply.

Studio and execution share the same static inspection. Invalid frontmatter,
names and same-scope duplicates fail before Run allocation. Inspection reads
Skill data without constructing models, loading tool implementations, connecting
MCP or executing Hooks. Platform activation retains the prepared body; new
inspections see disk edits while existing invocations retain their original
instructions. Resource file locations are sampled at activation, not frozen.

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
