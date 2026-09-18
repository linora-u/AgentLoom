# Architecture implementation task graph

Parent specification: [#67](https://github.com/linora-u/AgentLoom/issues/67),
[full contract](architecture-and-application-definition.md). Baseline: `ca27966d`.
The issue has no separate tracker tickets; these local tickets define the graph.
Every acceptance requirement in the specification remains mandatory.

Accepted scope update (2026-09-17): the user explicitly rejected legacy
compatibility. D1 must remove the old src package/entries and migrate every
owned caller to canonical owners. Existing behavioral tests and real
Applications remain mandatory; historical checkpoint data is preserved. The
final GitHub push waits for complete E1 acceptance and E2 review.

Source-layout selection: the user subsequently chose modules directly under
src/ (src/application, src/runtime, etc.), mapped by standard build configuration
to the installed agentloom package. No nested agentloom source folder and no
legacy src.* import support. Source location and Python package identity are
distinct; installation/source-origin/capsule contracts must cover this mapping.

| Ticket | Deliverable | Blocked by |
| --- | --- | --- |
| A1 | Baseline Python/TUI collection and results, ownership and public interface inventory, safe worktree configuration | — |
| A2 | Real architecture_contract_validation Application, native/CodeAct four-Worker tasks, independent oracle, nested/repeated/rejection variants, bounded reproducible runner | — |
| A3 | Controlled F3–F9 runners and independent validators; portable paths and historical checkpoint material | — |
| B1 | Shared lightweight definition loading, normalization, topology, configuration/provenance, paths and revision snapshots; runtime and Studio consumers | A1 |
| C1 | Move project-owned runtime responsibilities out of smolagents integration; preserve behavior and identity | A2, B1 |
| D1 | Canonical-only agentloom namespace; remove legacy src package/aliases/entries; migrate owned callers, Applications, tests, docs/templates; package resources and CLI/TUI/scaffold/dynamic import installation matrix | C1 |
| E1 | Full final Python/TUI, installation and real F1–F9 acceptance on candidate revision, retained failure evidence | A3, D1 |
| E2 | Independent code review, fixes, affected validation, ready PR, local main integration and GitHub delivery | E1 |

Each implementer uses its own branch/worktree. A merger integrates completed
tickets into `codex/architecture-application`. Configuration secrets remain in
ignored files, never commits. Evidence and coordination notes live outside the
repository at `/Users/bytedance/code/data_clear/agentloom-architecture-notes/`;
publish only sanitized summaries. Reference checkouts and prior user runtime data
are not changed or removed. A ticket is complete only with its stated evidence.

## Delivery status

A1–D1 are implemented. E1 verification is complete; results and exact execution
revisions are recorded in [final validation](architecture-final-validation.md).
E2 Spec review has no remaining findings. Its one Standards finding, Skill
discovery of nested workflow definitions, is explicitly deferred by the maintainer
to [#69](https://github.com/linora-u/AgentLoom/issues/69). It is not silently waived
or represented as fixed in PR #68. The follow-up implementation and validation for
#69 are recorded in
[nested workflow discovery validation](nested-workflow-discovery-validation.md).
PR #68 contains the completed architecture implementation and its original records.
