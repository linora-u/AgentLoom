# AgentLoom repository synchronization

Develop in `linora-u/AgentLoom-private`. The private application source directory
is `applications/news_agent/`. Other tracked files are public, including this
automation; shared uv configuration is exported with private workspace metadata
removed. Deleted public files are deleted from both repositories.

Define private files/directories in `.github/public-sync.json`, under
`private_prefixes`. Paths are relative to the repository root. A directory entry
excludes all its descendants; a file entry excludes that exact file. Wildcards
are not supported. All other tracked files are exported to the public repository.
The exporter reads this setting from every source commit. Paths made private by
later commits in the same export are also hidden from earlier snapshots, so a new
private path cannot leak through an intermediate commit. Public CI and the local
push guard use the same configuration. Adding an already-published path may
require cleaning its public history before the public history guard can pass.

Root `pyproject.toml` workspace members and excludes under private paths are
removed from the public projection. Root `uv.lock` excludes private local
packages, their membership and references, and packages reachable only from
private applications. Public workspace members and framework dependencies stay
in the exported files. An equivalent public lock from source ancestry is reused
when adding a private member only changes lock formatting or redundant markers;
real framework dependency updates are still exported. This filtering applies to
every source snapshot, so intermediate private workspace additions are skipped
even if the source commits are already on private `main`. The public history
guard also rejects shared uv metadata referencing private paths.

The local checkout at `/home/lin/code/AgentLoom` uses `origin` for the private
repository and `public` for the open-source repository. Push development branches
to `origin` and open private PRs against `main`. The local pre-push hook rejects
private `news_agent` history if a branch is accidentally pushed to `public`;
the exporter creates separate commits with clean public parents.

Public commits preserve the source commit's complete message, author, and author
date. The committer uses `linora-u` and the verified GitHub noreply email
`260928258+linora-u@users.noreply.github.com`. Commit SHAs and signatures change
because the trees and parents are rewritten; private source parents are never
imported. Messages and author identities for commits with public changes, and
ready private PR titles, are deliberately published.

Open a private pull request against `main`. Draft PRs stay unpublished. A ready
PR is processed automatically:

1. Rebase/update the PR if private `main` has moved.
2. Compare the public files with the private PR's current base. If public `main`
   has independent changes, stop; bring those changes into private `main` first.
3. Rewrite each source commit containing public changes onto public parents.
   Preserve its message and author metadata, skip private-only commits, and use
   the private PR title for the public PR. Keep meaningful merges and conflict
   resolutions; collapse redundant merge wrappers. Branches started before the
   current private base attach to the matching older public snapshot.
4. Wait for `Python tests` and all three installation-profile checks to succeed.
5. Merge the exact tested public head with a merge commit, preserving the exported
   commits rather than squashing them, and verify its merged tree.
6. Mark the private head's `Public sync` status successful and merge that exact
   private head. A PR changing only `news_agent` needs no public PR or tests.

Private repository test jobs are disabled. This uses GitHub Free-compatible
Actions and the existing public branch protection. It does not enforce private
branch protection or prevent manual private merges. Keep routine changes on PR
branches so the coordinator can maintain the merge order.
Manually merging or directly pushing private `main` triggers a recovery run.
The coordinator exports unpublished commits with the same history-preserving
logic, creates a public PR if needed, and merges it only after all four public
checks pass. Public changes followed by a revert are exported even when the final
tree is unchanged. Changes only to private files need no public PR or tests.
Normal PR delivery still merges public first, then private;
recovery cannot enforce that order once private `main` has already been updated.
For recovery, public `main` must match the saved publication cursor or a previous
private `main` snapshot. The coordinator stops rather than overwriting independent
public changes. A private
main update while recovery CI is running invalidates that recovery attempt;
the next push, hourly recovery, or manual run resumes from the latest main.

Actions run from trusted private `main` and never execute private PR code. The
deploy key only pushes public sync branches. `PUBLIC_SYNC_TOKEN`, stored only in
the private repository, authenticates PR/status API operations. Ready private
PRs expose their public files when the public sync branch is created.

After publication, the coordinator saves the source head, public merge SHA, and
source-to-public commit mapping in `state.json` on the private metadata-only
branch `codex/public-sync-state`. This branch is outside `main` and does not
trigger a new sync run. The sync token needs private-repository contents write
access to maintain it. The public PR also carries the mapping in an HTML comment
so a retry can recover after public merge succeeded but state persistence failed.
Branch names include the source base and head, and commit creation is
deterministic, so retrying a pushed branch whose PR creation failed does not
duplicate commits. Do not edit generated sync branches or the state branch.

The first run without a saved cursor uses the previous snapshot-based recovery
boundary. Existing `Sync public files` commits are retained; the new behavior
applies to subsequent publications and does not rewrite public `main` history.

Use **Actions -> Public repository sync -> Run workflow** to retry one PR or all
ready PRs. Every run also checks private `main` for unpublished public changes,
even when no PRs are open. An hourly recovery run resumes interrupted operations. A public
merge followed by a private merge failure is resumable; the two merges are not
one transaction. Changes in either PR invalidate older checks.

The initial migration removes `news_agent` from public branches, tags, and their
history. Original history is backed up in the private repository. Future public
commits are generated with public parents and never copy private Git history.
