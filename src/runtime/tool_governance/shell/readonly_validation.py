"""Read-only command validation for shell tool.

Identifies commands that only read data and do not modify the filesystem,
allowing the security layer to apply lighter validation for read-only
operations (e.g., skip path write checks for ``cat``, ``grep``).
"""

import re
from typing import Set


# Commands that are inherently read-only (never modify filesystem)
READ_ONLY_COMMANDS: Set[str] = frozenset({
    # File reading
    "cat", "head", "tail", "less", "more", "tac", "nl",
    "wc", "md5sum", "sha1sum", "sha256sum", "sha512sum",
    "file", "stat", "strings", "hexdump", "od", "xxd",
    "base64",
    # Directory listing
    "ls", "tree", "du", "df", "find", "locate", "which", "whereis",
    "realpath", "readlink", "dirname", "basename",
    # Text search
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    # Text processing (read-only when no -i flag and no output redirect)
    "sort", "uniq", "cut", "paste", "column", "tr", "fold",
    "diff", "comm", "cmp",
    # Data processing
    "jq", "yq", "xq",
    # System info
    "date", "hostname", "uname", "whoami", "id", "groups",
    "uptime", "free", "top", "htop", "ps", "pgrep",
    "lsof", "netstat", "ss", "ip", "ifconfig",
    "env", "printenv", "set", "locale",
    # Version / help
    "man", "info", "help", "type", "command",
    # Git read-only
    "git status", "git log", "git diff", "git show",
    "git branch", "git tag", "git remote", "git stash list",
    "git blame", "git shortlog", "git rev-parse",
    # Package managers (query only)
    "pip list", "pip show", "pip freeze",
    "npm list", "npm ls", "npm view",
    "cargo metadata",
    # Misc
    "echo", "printf", "true", "false", "test", "[",
    "pwd", "tput", "tty",
})

# Git subcommands that are read-only
GIT_READ_ONLY_SUBCOMMANDS: Set[str] = frozenset({
    "status", "log", "diff", "show", "branch", "tag",
    "remote", "stash list", "blame", "shortlog", "rev-parse",
    "describe", "ls-files", "ls-tree", "ls-remote",
    "config --list", "config --get",
})

# Git subcommands that are NOT read-only (write operations)
GIT_WRITE_SUBCOMMANDS: Set[str] = frozenset({
    "push", "pull", "fetch", "merge", "rebase", "cherry-pick",
    "commit", "add", "rm", "mv", "reset", "clean", "checkout",
    "switch", "restore", "stash push", "stash pop", "stash drop",
    "init", "clone", "submodule",
})

# sed is read-only when used with -n for line printing only
_SED_PRINT_PATTERN = re.compile(
    r"^sed\s+(-[nEer]\s+)*'?\d*[,\d]*p'?\s+",
)

# sed is NOT read-only when -i (in-place) is used
_SED_INPLACE_PATTERN = re.compile(r'\bsed\b.*\s-i')


def is_read_only_command(command: str) -> bool:
    """Determine if a shell command is purely read-only.

    A command is read-only if ALL segments in a pipeline/chain are
    read-only operations.  A single write segment makes the whole
    command non-read-only.

    Args:
        command: The full shell command string.

    Returns:
        True if the command only reads data, False if it may modify state.
    """
    if not command or not command.strip():
        return True

    # Split into segments by pipe / && / || / ;
    segments = re.split(r'\s*(?:\|{1,2}|&&|;)\s*', command.strip())

    for segment in segments:
        if not segment.strip():
            continue
        if not _is_segment_read_only(segment.strip()):
            return False

    return True


def _is_segment_read_only(segment: str) -> bool:
    """Check if a single command segment (no pipes/chains) is read-only."""
    # Strip env var assignments
    stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', segment).strip()
    if not stripped:
        return True

    # Check for output redirection (>, >>)
    if re.search(r'(?<![12])>', stripped) or '>>' in stripped:
        return False

    # Get the base command name
    words = stripped.split()
    if not words:
        return True

    first_word = words[0]
    base_name = first_word.rsplit('/', 1)[-1] if '/' in first_word else first_word

    # Direct match against read-only commands
    if base_name in READ_ONLY_COMMANDS:
        return True

    # Special: git with read-only subcommand
    if base_name == "git" and len(words) > 1:
        subcommand = words[1]
        if subcommand in GIT_READ_ONLY_SUBCOMMANDS:
            return True
        if subcommand in GIT_WRITE_SUBCOMMANDS:
            return False
        # Unknown git subcommand — conservative, treat as write
        return False

    # Special: sed read-only detection
    if base_name == "sed":
        # -i flag means in-place edit (write)
        if _SED_INPLACE_PATTERN.search(segment):
            return False
        # sed -n 'Np' pattern is read-only (just printing)
        if _SED_PRINT_PATTERN.match(segment):
            return True
        # Unknown sed pattern — conservative
        return False

    # Special: awk without output redirect is read-only
    if base_name == "awk":
        return True

    # Unknown command — conservative, treat as possibly writing
    return False
