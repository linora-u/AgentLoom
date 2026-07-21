# AgentLoom Application Studio

`agentloom` is an Applications-first terminal control plane. It embeds a fixed
OpenCode Runtime for the Studio Agent Loop and uses AgentLoom's Python runtime
only for domain truth: configuration, validation, Effective Config, topology,
Run lifecycle, and evidence.

## Install, update, and run

```bash
cd /path/to/AgentLoom
./install
agentloom --project /path/to/AgentLoom
```

Rerun `./install` to update a source installation. After the first install,
`agentloom update` performs the same update from the recorded trusted checkout.
`./install --no-modify-path` is optional and only prevents the installer from
editing shell PATH configuration; it is not required for updates.

The TUI checks the recorded trusted source checkout after first render. When
relevant sources are newer than the installed compatible unit, `Ctrl+X` offers
an explicit update of TUI + OpenCode + Python followed by a safe restart. It
does not fetch Git or silently replace an active Session.

The installer builds and installs one compatible unit under `~/.agentloom`:
the TypeScript/OpenTUI binary, OpenCode Runtime `1.18.3`, and a locked Python
environment. `agentloom --snapshot` is the non-interactive health check.

## Product model

- The first entry is `+ New Application`, followed by every directory under
  `applications/`. Choosing another Application retargets the current Studio
  Session and keeps its conversation memory; only `/new` starts a fresh Session.
- The workspace summary counts Applications, not expanded Supervisor/Worker
  YAML definitions. Main Supervisor Agents are searchable through `Ctrl+X`;
  Worker Agents remain inspectable inside Application and main-Agent details.
- `Global Skills` counts runtime-global Skill manifests from the root `skills/`
  directory and explicit global configuration. Application-private Skills are
  shown only inside that Application and its Agent details. The framework Skill
  used by Studio is development context and is not counted as an Application Skill.
- The right panel shows Effective Config, Working Revision, Running Revision,
  Supervisor/Workers, model sources, Tools, Skills, permissions, Hooks, MCP,
  workflow source files, and validation. It does not expose model credentials.
- Recent Runs are secondary navigation. Their default detail is a compact,
  actionable summary; raw event objects and full log bodies are not dumped.

## Studio Agent Loop

The Studio Agent can read the project, edit the selected Application directly,
show OpenCode Tool and Diff blocks, validate, request permission to run, inspect
structured Run evidence, and continue fixing until the acceptance criteria are
met. There is no separate draft-write command in the normal workflow.

`Application Only` is the default: project reads and writes inside the selected
Application are allowed; Shell, global files, other Applications, and unknown
new-Application paths require OpenCode permission. Permission cards support
`1` once, `2` for this Session, and `3` reject. `Full Access` is available from
`Ctrl+X` as one on/off toggle. It can be preset before selecting an Application,
remains active while switching Applications, and resets when the TUI exits.
Select terminal text and press `Ctrl+Y` to copy it through OSC52.

OpenCode question requests are shown as decision cards. A user can click one
choice or type an answer; multiple answers are separated with `|`, and `Esc`
rejects the request. Set `AGENTLOOM_REDUCED_MOTION=1` to use static status
symbols; dumb and CI terminals select this mode automatically.

Model-facing Application detail is deduplicated and paginated instead of
placing a complete large Application in one Tool result. Studio follows the
OpenCode Session lifecycle for status, retry, permission, question, and Task
sub-session progress; quiet model latency is not treated as a cancellation
signal. `Esc` is the explicit manual interrupt. An interrupted unfinished turn is
removed from future model context, while file changes already completed by its
tools are retained; the next message therefore starts a new task instead of
silently resuming the cancelled one.

Runs pin the Application content hash in `manifest.json`. Later edits change the
Working Revision but never hot-switch the Running Revision. A new/restarted Run
is required to use new configuration.

Studio and Application Agents share the project-root `config/llm.yaml` as their
only model catalog, Provider, authentication, and default source. The Studio
maps those profiles into the bundled OpenCode Runtime; Application Agents keep
resolving them through Python and YAML `model_type`. Selecting a Studio model
with `/models` or `Ctrl+X` never changes an Application model. Missing or invalid
configuration is an explicit startup error, not an OpenCode default fallback.

## Navigation

| Action | Key / command |
|---|---|
| Send a Studio message | `Enter` |
| Search commands and global entities | `Ctrl+X` |
| Select a Studio model from `config/llm.yaml` | `/models` or `Ctrl+X` |
| Re-index project state | `/refresh` |
| Scroll chat or the focused detail | `PgUp` / `PgDn`; mouse wheel scrolls the pointed region |
| Diagnose a selected failed Run | `a` |
| Close detail / reject permission or question / interrupt active loop | `Esc` |
| Exit | `Ctrl+C` |

There is intentionally no global `?` binding. Discoverability lives in the
visible footer, `/help`, and the `Ctrl+X` command descriptions.

Schedules remain a separate foreground service:

```bash
agentloom schedules --project /path/to/project serve
```

## Architecture

```text
OpenTUI / SolidJS
  ├─ OpenCode SDK + fixed OpenCode Runtime
  │    ├─ Session / Agent Loop / LLM / Tool / Diff / Permission
  │    └─ agentloom_domain Tool
  └─ long-lived Python NDJSON bridge
       └─ AgentLoom Effective Config / catalog / Run evidence

agentloom_domain
  └─ python -I -m src.tui_bridge.domain_cli
       ├─ application.detail / validate / impact
       └─ run.start / stop / resume / restart / detail
```

The OpenTUI presentation adaptation retains OpenCode's MIT provenance under
[`upstream/`](upstream/README.md). Studio's general Agent Loop is OpenCode's;
AgentLoom does not implement a second permission or session state machine.

## Development

```bash
bun install --frozen-lockfile
bun test
bun run typecheck
bun run build

cd ..
.venv/bin/pytest -q tests/tui_bridge_test
```
