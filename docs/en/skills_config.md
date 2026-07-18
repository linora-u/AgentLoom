# Skills Configuration

AgentLoom skills use Claude Code style packages:

```text
skills/<skill-name>/SKILL.md
skills/<skill-name>/references/
skills/<skill-name>/scripts/
skills/<skill-name>/assets/
```

`SKILL.md` is matched case-insensitively (`SKILL.md` or `skill.md`). Loose Markdown files and `skills.md` are not loaded.

## SKILL.md Frontmatter

Required fields:

```yaml
---
name: test-driven-development
description: Test-driven development workflow.
---
```

Supported optional fields:

```yaml
allowed-tools: Bash, Read, Edit
argument-hint: "<task>"
arguments: [task]
when_to_use: Use when implementing or fixing behavior with tests.
model: powerful
context: fork
agent: reviewer
effort: high
shell: bash
```

Unknown frontmatter fields are ignored. `hooks` is rejected with a migration error because Hooks use the independent top-level [`hooks`](hooks.md) configuration. Legacy fields such as `when-to-use`, `argument-names`, `requires`, `disable-model-invocation`, and `user-invocable` are not mapped.

Invalid YAML, missing `name`, missing `description`, invalid names, and duplicate skill names are errors.

## Agent YAML

Default loading is on-demand:

```yaml
skills:
  load-mode: on-demand
  items:
    - skills/tdd
    - skills/debugging
```

Eager loading injects the full skill body into the system prompt:

```yaml
skills:
  load-mode: eager
  items:
    - skills/strict-review
```

Per-item overrides are allowed:

```yaml
skills:
  load-mode: on-demand
  items:
    - skills/tdd
    - path: skills/strict-review
      load-mode: eager
```

Shorthand forms still work:

```yaml
skills: skills/tdd

skills:
  - skills/tdd
  - path: skills/debugging
```

## Loading Semantics

- Configured skills are registered at agent startup.
- `on-demand`: the system prompt contains only a lightweight catalogue with `name`, `description`, `argument_hint`, and `when_to_use`.
- `eager`: the full skill body is injected into `<eager_loaded_skills>` and is not repeated in the catalogue.
- There is no hidden, user-invocable, force-inject, or invocation-control state.
- `list_skills(detail="full")` lists all configured skills and their runtime policy.
- `load_skill` returns a deduplication notice for eager skills because the body is already in context.

## Resources

Use `read_skill_resource(skill, path, offset, limit)` to read files bundled with a skill. Paths are resolved inside the skill directory; directory escape is rejected.

## Scripts

Third-party scripts are allowed by default:

```python
run_skill_script("youtube-transcript", "npm install")
run_skill_script("youtube-transcript", "node transcript.js EBw7gsDPAYQ")
```

Execution audit logs include command, cwd, environment names, exit code, stdout/stderr paths, and the audit directory.

Users can explicitly restrict behavior:

```yaml
skills:
  load-mode: on-demand
  allow-scripts: false
  allow-network: false
  items:
    - skills/safe-review
```

`allow-scripts: false` blocks script execution. `allow-network: false` blocks common network commands such as `curl`, `wget`, `ssh`, `npm`, `pip`, `pnpm`, and `yarn`.
