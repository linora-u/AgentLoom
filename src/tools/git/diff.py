
def get_git_diff_content(repo_path: str, source_commit: str,
                         target_commit: str) -> str:
    """
    Get diff content between two Git commits.

    Run the `git diff` command to compare code changes between two commits and
    return the full diff text.
    The diff includes added, deleted, and modified files with detailed content changes.

    Args:
        repo_path: Repository path, e.g. '/xx/xx/xx'.
        source_commit: Source commit identifier (hash, branch, tag, or HEAD ref).
        target_commit: Target commit identifier (hash, branch, tag, or HEAD ref).

    Returns:
        str: Full diff text in unified diff format.
             Returns an empty string if there is no diff between the two commits.

    Example:
    >>> diff = get_git_diff_content('/path/to/repo', 'HEAD~1', 'HEAD')
    'diff --git a/file.txt b/file.txt\nindex abc123..def456 100644\n--- a/file.txt\n+++ b/file.txt\n@@ -1,3 +1,4'
    """
    import subprocess
    import logging

    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "diff", source_commit, target_commit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        logging.info("diff result stdout: %s", result.stdout)
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git diff stats command failed: {e.stderr}") from e
