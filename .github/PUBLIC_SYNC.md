# AgentLoom repository synchronization

Develop in `linora-u/AgentLoom-private`. The only private source directory is
`applications/news_agent/`. All other tracked files are public, including this
automation. Deleted files are deleted from both repositories.

Open a private pull request against `main`. Draft PRs stay unpublished. A ready
PR is processed automatically:

1. Rebase/update the PR if private `main` has moved.
2. Compare the public files with the private PR's current base. If public `main`
   has independent changes, stop; bring those changes into private `main` first.
3. Create a public-only commit and PR. Private commit history, PR titles, and
   commit messages are not copied to the public repository.
4. Wait for `Python tests` and all three installation-profile checks to succeed.
5. Merge the exact tested public head and verify its merged tree.
6. Mark the private head's `Public sync` status successful and merge that exact
   private head. A PR changing only `news_agent` needs no public PR or tests.

Private repository test jobs are disabled. This uses GitHub Free-compatible
Actions and the existing public branch protection. It does not enforce private
branch protection or prevent manual private merges. Keep routine changes on PR
branches so the coordinator can maintain the merge order.

Actions run from trusted private `main` and never execute private PR code. The
deploy key only pushes public sync branches. `PUBLIC_SYNC_TOKEN`, stored only in
the private repository, authenticates PR/status API operations. Ready private
PRs expose their public files when the public sync branch is created.

Use **Actions -> Public repository sync -> Run workflow** to retry one PR or all
ready PRs. An hourly recovery run also resumes interrupted operations. A public
merge followed by a private merge failure is resumable; the two merges are not
one transaction. Changes in either PR invalidate older checks.

The initial migration removes `news_agent` from public branches, tags, and their
history. Original history is backed up in the private repository. Future public
commits are generated with public parents and never copy private Git history.
