# AgentLoom TUI acceptance contract

1. Source lives in the AgentLoom project root under `agentloom-tui/`; the
   supplied `opencode/` checkout remains unchanged and is used only as the
   upstream source reference.
2. The implementation uses TypeScript, SolidJS, and OpenTUI. Reused OpenCode
   code records its MIT license, exact upstream commit, and source mapping.
3. The `agentloom` command opens a simple Builder conversation. Its model list
   and default come from `config/llm.yaml` without exposing secrets.
4. Builder ReAct is bounded to inspect, stage Agent YAML in memory, and validate.
   It cannot execute Shell, Git, long coding tasks, or Agents. Only explicit
   `/apply` writes a valid, revision-matched draft.
5. The TUI lists every supervisor Agent System in the current project and all
   canonical Runs, distinguishing `never_run`, `running`, `completed`, `failed`,
   and `crashed`.
6. Agent Systems and Runs are mouse-clickable and keyboard-selectable. A
   never-run detail shows definition, files, topology, validation, and no fake
   result. A Run detail shows Workers, events, logs, artifacts, and result state.
   Refresh work and response size stay bounded; every partial preview carries
   explicit truncation metadata and is labeled in the UI.
7. Current execution state refreshes without losing the selected detail. Wide
   terminals use OpenCode's 42-column right sidebar; narrow terminals use an
   overlay.
8. Completed runs retain result and task events even when successful checkpoint
   cleanup is enabled.
9. Unit, type, bridge, real-project snapshot, and interactive TUI smoke checks
   must pass before delivery.
