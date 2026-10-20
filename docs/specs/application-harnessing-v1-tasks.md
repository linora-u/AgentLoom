# Application Harness implementation task graph

Source: public Issue #136 and the adjacent application-harnessing-v1 specification.
Delivery branch: codex/application-harnessing-136. One private delivery PR; public framework projection follows the existing repository sync workflow.

| Ticket | Depends on | Deliverable and evidence |
| --- | --- | --- |
| H1 | — | Strict Application harnessing policy; immutable task, candidate, trial and experiment contracts; source validation and public configuration inspection. |
| H2 | H1 | Complete portable dependency snapshots; immutable candidate changes; physical subprocess and data visibility isolation; original host binding and cleanup. |
| H3 | H1, H2 | Persistent controller: upstream parallel/sequential recursion, built-in/custom improvement roles, feedback, finals-only selection, library, usage, stop and recovery. |
| H4 | H2, H3 | Public complete-task entry and Supervisor/CLI integration; real nested Agent execution; unchanged disabled behavior. |
| H5 | H2, H3 | Program-driven news adapter; one-time factual preparation; configuration-driven foreground/background orchestration; isolated trials, acceptance and idempotent adoption. |
| H6 | H4, H5 | Complete public-entry acceptance matrix, both runtimes and complete news integration; real validation evidence and requirement audit. |
| H7 | H6 | Code review, fixes, checks, PR ready for review and temporary implementer worktree cleanup. |

Status is recorded by commits and acceptance evidence, not by assuming a task complete when a file exists. No implementation ticket may reduce Issue #136's scope. Deterministic framework tests must remain independent of the private news application. Hidden evaluation, physical cleanup, and safe recovery require actual behavioral evidence.
