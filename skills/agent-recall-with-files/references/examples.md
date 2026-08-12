# Examples

## Example 1: First Entries After Task Start

```md
# Context

## Goal
Refactor the user authentication module to support OAuth2 token refresh.

## Current Status
Starting. Reading existing auth flow to understand the current token lifecycle.

## Remaining
- Read current auth module structure
- Identify token refresh integration points
- Implement refresh logic
- Add unit tests
- Update API docs

## Key Files
- `src/auth/token_manager.py`
- `src/auth/oauth_client.py`
- `tests/auth/test_token_manager.py`
```

```md
# Trace

## Log
- [2026-03-13 14:00:00] Task started. Goal: add OAuth2 token refresh to auth module.
- [2026-03-13 14:02:00] Reading `src/auth/token_manager.py` to map current token lifecycle.
```

Replace template placeholders immediately. Do not leave them while you start working.

## Example 2: After Creating an Artifact

```md
# Trace

## Log
- [2026-03-13 14:00:00] Task started. Goal: add OAuth2 token refresh to auth module.
- [2026-03-13 14:02:00] Reading `src/auth/token_manager.py` to map current token lifecycle.
- [2026-03-13 14:18:00] Wrote `src/auth/token_refresher.py` with refresh logic and retry backoff. Next: wire it into `oauth_client.py` and add tests.
```

```md
# Insights

## Log
- [2026-03-13] [fact] `src/auth/token_refresher.py` created with exponential backoff (base 1s, max 30s).
- [2026-03-13] [fact] `TokenManager.get_token()` is called from 12 places across 4 modules.
```

## Example 3: Recording a Pitfall

```md
# Insights

## Log
- [2026-03-13] [pitfall] `TokenManager` is a singleton but not thread-safe. Two concurrent requests can trigger duplicate refresh calls, causing a 401 race. Must add a lock around the refresh path.
- [2026-03-13] [decision] Use `threading.Lock` in `TokenManager.refresh()` rather than making the entire class thread-safe — keeps the change scoped.
- [2026-03-13] [dependency] The `httpx` library (v0.27+) is required for async token refresh. Older versions lack `AsyncClient.auth` hook support.
```

## Example 4: Cross-Session Recall

On the second run, the agent sees prior insights injected by `PreToolUse`:

```
[agent-recall-with-files] Runtime context from .agentloom/workspaces/agents/example/auth_refactor_agent/tasks/task_123/:

[context.md]
# Context
## Goal
Fix flaky auth tests after the OAuth2 token refresh refactor.
...

[insights.md]
- [2026-03-12] [pitfall] `TokenManager` is a singleton but not thread-safe. Two concurrent requests can trigger duplicate refresh...
- [2026-03-12] [dependency] The `httpx` library (v0.27+) is required for async token refresh...
```

The agent immediately knows to check thread-safety in the test setup, and avoids wasting time on the `httpx` version issue that was already resolved.

## Example 5: No New Insights

Leave `insights.md` unchanged. Record task completion in `trace.md`; the durable
file should contain only knowledge that another task can reuse.
