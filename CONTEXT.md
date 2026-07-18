# AgentLoom Hook Runtime

This context defines the project language for code that observes or governs an
Agent invocation.

## Language

**Hook Event**:
A named runtime seam where configured behavior may observe an invocation or
govern a pending action.
_Avoid_: callback type, lifecycle signal

**Hook Spec**:
One validated Shell declaration with a stable ID, event, matcher, command,
timeout, cwd, and source provenance. A Spec is inert until a Hook Plan is
compiled.
_Avoid_: Skill metadata, registered callback

**Hook Bundle**:
An explicitly referenced directory containing a root `HOOK.yaml` and its
scripts or resources. Bundle directories are never auto-discovered.
_Avoid_: Skill, implicit plugin

**Hook Handler**:
One compiled, ordered implementation of a Hook Spec or trusted framework
observer.
_Avoid_: global callback, Hook command dictionary

**Hook Plan**:
The immutable effective Handler sequence compiled from global, application,
and Agent Hook configuration. A Plan retains stable IDs, source provenance,
and a deterministic fingerprint.
_Avoid_: Hook registry, mutable global configuration

**Hook Run**:
The isolated invocation that applies a Hook Plan and owns effects, diagnostics,
metrics, and root/local run identity.
_Avoid_: Hook manager, process-global state

**Skill**:
Prompt instructions, resources, and explicitly invoked Skill scripts. A Skill
is not a Hook source; `SKILL.md` must never declare or register Hooks.

## Runtime invariants

- Only direct `hooks:` configuration and explicitly referenced Hook Bundles
  authorize Shell Hook execution.
- Configured Handlers execute sequentially. `PreToolUse` transformations feed
  the next Handler and a block short-circuits the chain.
- Transformed tool input is strictly decoded again before `CoreToolGuard`, file
  history, or any tool side effect.
- `CoreToolGuard` and final-input recording are fixed runtime seams, not
  replaceable Hook IDs.
- Every tool wrapper requires an explicitly bound Hook Run. Missing context is
  a framework error, never a reason to bypass Hooks.

## Trust model

An explicitly configured Shell Hook is trusted local code, not a sandboxed
tenant. AgentLoom filters sensitive inherited environment variables and assigns
the process tree a unique marker so timeout cleanup can terminate ordinary
forks, new sessions, and reparented descendants. The Hook schema deliberately
does not claim portable network isolation.
