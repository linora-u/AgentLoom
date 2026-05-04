
def git_grep_files(
    repo_path: str,
    commit: str,
    words: str,
) -> str:
    """
    Full-text search file paths containing keywords in a specified Git commit.

    Use `git grep` to run full-text search in the repository and return files
    containing the specified keywords.
    Search is case-insensitive and returns file paths only, without matched content.

    Args:
    repo_path: Repository path, e.g. '/xx/xx/xx'
    words: Search keywords (string), regex supported, multiple keywords separated by spaces
    commit: Target Git commit identifier (hash, branch, or tag)

    Returns:
        list[str]: File paths containing the keywords, sorted alphabetically.
                Returns an empty list if no match is found.

    Example:
        >>> git_grep_files('/path/to/repo', 'main', 'getUserInfo')
        ['src/auth.go', 'src/user/profile.js']
    """
    import subprocess
    import logging

    if not repo_path:
        raise ValueError("repo_path is required")
    if not commit:
        raise ValueError("commit is required")
    if not words:
        raise ValueError("words is required")
    cmd = [
        "git",
        "-C",
        repo_path,
        "grep",
        "--full-name",  # Show paths relative to repository root.
        "--name-only",  # Output file names only, without matched content.
        "-l",  # Show only file names that contain matches.
        words,
        commit,
    ]
    logging.info("git grep cmd: %s", cmd)
    try:
        # Run the command and capture output.
        result = subprocess.run(cmd,
                                check=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True)
        # Split output by lines and deduplicate.
        files = set()
        for line in result.stdout.splitlines():
            # Split format example: commit:path:line:content -> take path part.
            path = line.split(":", 2)[1]  # Split twice and keep the second part.
            files.add(path)
        return sorted(files)

    except subprocess.CalledProcessError as e:
        if e.returncode == 1:  # git grep found no match
            return ""
        raise RuntimeError(f"git grep execution failed: {e.stderr}") from e


def is_path_in_repo(repo_path: str, file_path: str, commit: str) -> bool:
    """
    Check whether a specified file path exists in a specific Git commit.
    Uses `git ls-files --with-tree={commit}` to check whether the file is tracked.

    Args:
        repo_path: Repository path, e.g. '/xx/xx/xx'
        file_path: Relative file path from repository root, e.g. 'xx/xx/xx.go'
        commit: Target Git commit identifier (hash, branch, or tag)

    Returns:
        bool: Returns True if the file exists in the specified commit, otherwise False.

    """

    from pathlib import Path
    import subprocess
    import logging

    if not repo_path:
        raise ValueError("repo_path is required")
    if not file_path:
        raise ValueError("file_path is required")
    if not commit:
        raise ValueError("commit is required")
    path = str(Path(file_path).as_posix())

    cmd_tracked = [
        "git",
        "-C",
        repo_path,
        "ls-files",
        f"--with-tree={commit}",
        "--",
        path,
    ]
    logging.info("cmd_tracked: %s", cmd_tracked)
    res = subprocess.run(
        cmd_tracked,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not res.stdout:
        return False
    return True
