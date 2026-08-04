# Goal Mode Validation Application

This Application exercises Goal Mode with real model and Worker calls against the
current AgentLoom checkout. Generated reports go to ignored `outputs/`; durable
Goal evidence lives in `.agentloom/runs` and `.agentloom/checkpoints`.

## Scenarios

| Workflow | Goal config | Purpose | Expected first outcome |
|---|---|---|---|
| `goal_unlimited_endurance_agent.yaml` | `goal: true` | 16 sequential specialist audits plus synthesis; intended 30-minute-class validation | `complete` |
| `goal_bounded_list_agent.yaml` | mapping, 600000 tokens | workflow list merged into one context, Worker accounting, explicit completion | `complete` |
| `goal_parallel_budget_agent.yaml` | mapping, 50000 tokens | six parallel Worker calls cross a shared soft budget | `budget_limited` |

Only the endurance scenario is unlimited. The other Applications set explicit
limits. Goal configuration appears only in Supervisor YAML; every Worker omits the
key entirely.

## Validation commands

```bash
uv run python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/goal_mode_validation

uv run loom run \
  applications/goal_mode_validation/workflows/goal_bounded_list_agent.yaml \
  --output-format jsonl

uv run loom run \
  applications/goal_mode_validation/workflows/goal_parallel_budget_agent.yaml \
  --output-format jsonl
```

The parallel run should exit `1` with `run.budget_limited`. Record its `task_id`,
then either increase `token_budget` above `used_tokens` or remove the field and run:

```bash
uv run loom run \
  applications/goal_mode_validation/workflows/goal_parallel_budget_agent.yaml \
  --resume <task_id> --output-format jsonl
```

Resume must retain the same `task_id`, create a new `run_id`, avoid rerunning the
batch, and finish with the existing cumulative usage. An unchanged exhausted
budget must remain `budget_limited`.

For the long validation:

```bash
uv run loom run \
  applications/goal_mode_validation/workflows/goal_unlimited_endurance_agent.yaml \
  --output-format jsonl
```

After every run inspect `manifest.json`, `audit/goal.json`, runtime log, task
events, Worker call checkpoints, and the report under this Application's
`outputs/`. A successful Goal must contain non-empty completion evidence; a
budget-limited Goal must retain its task checkpoint.

## Recorded real-model acceptance run

The implementation was exercised against real configured models on 2026-08-05:

| Scenario | Task / run evidence | Observed result |
|---|---|---|
| Bounded workflow list | `task_20260804T170322505383Z_ce6d1069128f` / `run_20260804T170322505417Z_2cf0cabe1e45` | `complete`; numbered workflow merged into one objective; 427317 whole-tree tokens used from a 600000 budget |
| Parallel soft budget | `task_20260804T160413595075Z_01366ffd580c` / `run_20260804T160413595101Z_a7fadf21dc1f` | six concurrent Workers crossed 50000 tokens and produced `budget_limited` with 79190 tokens used |
| Parallel resume | same task / `run_20260804T160808447806Z_d3e1886fb5e8` | removing the cap resumed the same Goal and completed without rerunning the durable batch |
| Unlimited endurance | `task_20260804T171358741591Z_4e38fa57de4b` / `run_20260804T172155752890Z_cec594f24de9` | `complete` after about 29 minutes; an interrupted first attempt resumed under the same Goal; 2584069 tokens used with `token_budget: null` |
| Goal disabled regression | `task_20260804T174338049253Z_9414de2529c9` / `run_20260804T174338049282Z_c0027ab1580d` | ordinary workflow returned `TODO_AUTO_TRIVIAL_OK`; manifest omitted the Goal payload |

The generated Markdown reports and runtime directories are intentionally ignored
artifacts. The identifiers above make the local manifests and logs discoverable
without committing model output.
