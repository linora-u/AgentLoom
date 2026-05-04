

def get_git_file_content(repo_path: str, file_path: str, commit: str = "HEAD") -> str:
    """
    Read the content of a specified file from a Git repository.

    Use `git show` to read the full content of a file in a specific commit.
    Supports reading from any commit, branch, or tag.

    Args:
        repo_path: Repository path, e.g. '/xx/xx/xx'.
        file_path: Relative file path from repo root, e.g. 'xx/xx/xx.py'.
        commit: Git commit identifier (hash, branch, tag, or HEAD ref). Defaults to HEAD.

    Returns:
        str: Full file content text.
             Raises RuntimeError if the file does not exist.

    Example:
        >>> content = get_git_file_content('/path/to/repo', '.codedoggy_config.yaml', 'HEAD')
        >>> 'review:\n  enabled: true\n  max_comments: 10'
    """
    import subprocess
    import logging

    if not repo_path:
        raise ValueError("repo_path is required")
    if not file_path:
        raise ValueError("file_path is required")
    if not commit:
        raise ValueError("commit is required")

    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "show", f"{commit}:{file_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        logging.info("Successfully read file %s from commit %s", file_path, commit)
        return result.stdout
    except subprocess.CalledProcessError as e:
        if "does not exist" in e.stderr or "Path" in e.stderr:
            raise RuntimeError(f"File {file_path} does not exist in commit {commit}") from e
        else:
            raise RuntimeError(f"Failed to read Git file content: {e.stderr}") from e


def check_git_file_exists(repo_path: str, file_path: str, commit: str = "HEAD") -> bool:
    """
    Check whether a specified file exists in a Git repository.

    Use `git cat-file` to check whether a file exists in a specific commit.

    Args:
        repo_path: Repository path, e.g. '/xx/xx/xx'.
        file_path: Relative file path from repo root, e.g. 'xx/xx/xx.py'.
        commit: Git commit identifier (hash, branch, tag, or HEAD ref). Defaults to HEAD.

    Returns:
        bool: True if the file exists, otherwise False.

    Example:
        >>> exists = check_git_file_exists('/path/to/repo', '.codedoggy_config.yaml', 'HEAD')
        >>> True
    """
    import subprocess


    if not repo_path:
        raise ValueError("repo_path is required")
    if not file_path:
        raise ValueError("file_path is required")
    if not commit:
        raise ValueError("commit is required")

    try:
        subprocess.run(
            ["git", "-C", repo_path, "cat-file", "-e", f"{commit}:{file_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
