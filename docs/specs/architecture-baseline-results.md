# Architecture migration baseline

Source revision: `ca27966d`; tested from isolated worktree whose only added commits
were specification/inventory documentation. Recorded 2026-09-17, macOS arm64,
Python 3.12.13, locked project dependencies. Local ignored model configuration
was copied into the worktree; no credentials are included in this report.

| Check | Result |
| --- | --- |
| CI Python suite including both extra memory campaign contract files | 3769 collected; 3767 passed, 2 pre-existing skips, 0 failures/errors; 229.50 s |
| TUI tests | 199 passed, 0 failed, 751 assertions |
| TUI typecheck | Passed |
| TUI build | Passed |
| Wheel build and dependency-isolated wheel installation | Passed |
| Installed wheel CLI from outside source checkout | `python -I -m src --help` passed |
| Wheel prompt and tree-sitter query resources | Both present and readable |
| Installed wheel isolated Studio bridge | `python -I -m src.tui_bridge`, bootstrap RPC succeeded |

Raw baseline logs, JUnit XML, installed wheel and environment are retained in
`/Users/bytedance/code/data_clear/agentloom-architecture-notes/`, named
`baseline-*`. These results are a baseline, not final candidate acceptance.

## Observed gaps

- Default local uv interpreter selection chose Python 3.14 and failed compiling
  the pinned third-party sqlean dependency. Repeating setup with CI's Python 3.12
  succeeded without dependency changes.
- Installed configuration discovery from an unrelated cwd has no explicit project
  selection surface. CLI help and project-cwd bridge work; actual outside-project
  execution needs project context through supported discovery or an explicit path.
- Studio uses permissive YAML loading and a separate filtered configuration merge;
  runtime uses strict duplicate-key loading. Todo can disappear in the Studio
  projection. Existing tests do not yet assert the unified target contract.
- Runtime preflight checks the Supervisor before allocating a Run, but does not
  completely reject the Worker graph there. Path-only Worker definition caching
  can return stale content.
- Existing real acceptance runners contain developer-specific paths and obsolete
  `skills: []` input. Unit Test Studio's old smoke checks text rather than running
  generated pytest; its exception and Python-literal generation require validation.

New A2/A3 runners collect real-model evidence in separate controlled directories.
Their results, including failures and baseline checkpoints for historical resume,
will be reported separately; no unexecuted scenario is marked passed here.
