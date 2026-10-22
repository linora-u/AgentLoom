# Harness² upstream reuse implementation task graph

Source: public Issue #142 and the adjacent upstream-reuse v2 specification.
Delivery: one private branch and draft PR; preserve separate commits for coherent changes.

| Ticket | Depends on | Deliverable and evidence |
| --- | --- | --- |
| R1 | — | Vendored pinned upstream source, provenance/license, installed packaging, and minimal Pi/Application/feedback extension seams; upstream checks pass. |
| R2 | R1 | Replace the local method controller with upstream scheduling; Pi file-edit sessions, Application surface/execution adapter, explicit development feedback, experiment Library, and preserved isolation/resume/usage. |
| R3 | R2 | News adapter and ordered day sequence use the generic contracts; existing evaluator, four duties, acceptance and publication remain business-owned. |
| R4 | R2 | Public complete-task behavioral matrix, installed-package/Pi validation, stop/resume and feedback isolation; remove assertions tied to the replaced method implementation. |
| R5 | R3, R4 | Full news-date and cross-date acceptance, exact dependency/usage records, docs and requirement-by-requirement completion audit. |
| R6 | R5 | Standards/spec reviews against dc090494, one implementer fixes review findings, required checks, PR ready for review, implementer worktree cleanup. |

R1 and R2 may develop against an explicitly agreed extension contract in parallel, but R2 is not complete until integrated with R1. R3 can prepare and verify existing business contracts before R2 lands; actual integration and acceptance depend on R2. R4 uses the existing public Application Task entry point and controlled model service. R5 must distinguish real model evidence from controlled fixtures, and engineering correctness from measured improvement.

Upstream owns method stage decisions. AgentLoom owns frozen resources, trusted business execution/acceptance, private capability binding, physical lifecycle, exact usage observations and idempotent publication. An upstream patch must not introduce a second method state machine or news-specific code.

Ticket status is proven by commits and execution evidence, not by filenames or intended behavior. Historical user workspace deletions are excluded from this branch.
