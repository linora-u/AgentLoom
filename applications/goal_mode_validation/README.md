# Goal Mode validation

These Applications exercise persistent objectives, Worker delegation, explicit
completion, and checkpoint resume. Goal has no cost or token budget.

| Workflow | Coverage |
|---|---|
| `goal_workflow_list_agent.yaml` | Numbered workflow list, four Workers, report verification, explicit completion |
| `goal_parallel_agent.yaml` | Six parallel Workers, Goal-bound report reuse, explicit completion |
| `goal_unlimited_endurance_agent.yaml` | Sixteen specialist audits and synthesis across continuation segments |

Run through the public Application entrypoint:

```bash
uv run loom run applications/goal_mode_validation/workflows/goal_workflow_list_agent.yaml
uv run loom run applications/goal_mode_validation/workflows/goal_parallel_agent.yaml
```

Every successful run must have a completed Goal with evidence, persisted report
markers, and completed Worker calls. Interrupted work resumes with `--resume
<task_id>`; completed reports are reused for the same Goal. Legacy `token_budget`
is silently ignored, including on resume. Ordinary model usage remains audited.

The following results predate budget removal and are historical evidence only.

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
