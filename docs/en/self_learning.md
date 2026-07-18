# Self-Learning v6

AgentLoom separates searchable execution history from curated memory. History records what happened; curated memory contains only reviewed facts and experiences that may be injected into later runs. The database is authoritative. Markdown review artifacts are a human review surface, not a second state store.

## Configuration

```yaml
self_learning:
  enabled: true
  events_retention_days: 90
  memory:
    prompt_max_chars: 12000
    max_item_chars: 4000
    scope_budgets:
      project: 8000
      application: 6000
  review:
    enabled: true
    application:
      review_model: summary
      trigger: {mode: batch, min_completed_runs: 5}
      approval: {fact: auto, experience: manual}
    project:
      review_model: summary
      trigger: {mode: batch, min_candidates: 5}
      approval: {fact: manual, experience: manual}
    artifacts:
      markdown: true
      review_auto_applied: true
```

`events_retention_days` is currently a reserved compatibility value. Pruning is explicit: `loom sessions prune --retention-days N` uses the CLI value.

`trigger.mode` is `manual`, `after_run`, or `batch`. Manual review runs only through the CLI. After-run review runs after a successful root run when review context exists. Batch review waits for the configured completed-run or candidate threshold.

Approval is configured independently for `fact` and `experience`. `auto` still requires the code evidence gate and available capacity; accepted additions enter `active_unreviewed` and remain available for human `acknowledge` or `revoke`. `manual` additions remain `pending_pre_review` until the scoped inbox is applied.

Within `self_learning.review`, an Application or Agent overlay may change only `application`; `review.enabled`, `review.project`, and `review.artifacts` are project-root decisions. Other `self_learning` fields continue to follow the normal overlay contract.

## Scope and Candidate Contract

The model-facing `memory` tool supports only `list` and `propose`:

- `list` may inspect Project and the current Application memory.
- `propose` always creates a candidate for the current Application. It cannot target Project scope.
- A fact payload is exactly `{text}`.
- An experience payload is exactly `{trigger, symptom, action, verification}`.

The reviewer is a bounded extractor. It cannot choose scope or approval policy, replace or remove memory, promote to Project scope, or write files and Skills. Application review reads completed root runs for that Application plus trusted evidence. Project review reads only code-marked Project evidence and corroborated typed Application memory from at least two distinct Applications; it does not consume raw Application transcripts.

Searchable history never becomes deterministic fallback memory. Use `loom sessions search`, `scroll`, `index`, and `prune` to manage it.

## Human Review and Promotion

With the default `review.artifacts.markdown: true`, review files are stored under:

```text
.agentloom/reviews/
├── applications/<application_id>/
│   ├── batches/<review_id>/{review.json,REPORT.md}
│   └── INBOX.md
├── project/
│   ├── batches/<review_id>/{review.json,REPORT.md}
│   └── INBOX.md
└── INDEX.md
```

Batch files are immutable evidence. Edit only the scoped `INBOX.md`, then apply it. With `markdown: false`, the equivalent workflow uses immutable `review.json`, editable `INBOX.json`, and `INDEX.json`; no `REPORT.md` is created. Supported decisions include `approve`, `acknowledge`, `reject`, `revoke`, `correct`, and `promote_project`; candidate id and revision checks prevent stale edits.

Project promotion is deliberately human-only. `promote_project` must start from an Application candidate and pass activation-evidence checks. A successful promotion creates an `active_confirmed` Project item and marks the Application item `shadowed`; a conflicting Project payload for the same key is rejected.

```bash
uv run loom learn review --application <id>
uv run loom learn review --project
uv run loom learn review --all-unreviewed --dry-run
uv run loom reviews status --application <id>
uv run loom reviews apply --application <id>
uv run loom reviews rollback <review_id>
```

`loom memory list/add/replace/remove/pending/stats/export` is the administrator interface for direct active-memory maintenance. It is intentionally different from the proposal-only model tool. There are no `loom memory approve` or `loom memory reject` commands.

## Migration from v5

The validator rejects `self_learning.memory.review_model` and `write_approval`. Move model selection to `review.application.review_model` and `review.project.review_model`, and replace the global write flag with each scope's `approval.fact` and `approval.experience`. Process pending review work through scoped inboxes and `loom reviews apply`.
