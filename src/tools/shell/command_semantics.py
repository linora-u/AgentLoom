"""Shell command exit code semantic interpretation.

Many commands use non-zero exit codes to convey information
beyond simple success/failure.  For example, ``grep`` returns 1
when no matches are found — this is not an error condition.

This module provides contextual interpretation so the LLM
does not misinterpret informational exit codes as failures.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ExitCodeInterpretation:
    """Semantic interpretation of a command's exit code."""
    is_error: bool
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-command semantics
# ---------------------------------------------------------------------------

def _grep_semantic(exit_code: int) -> ExitCodeInterpretation:
    """grep/rg: 0=matches found, 1=no matches, 2+=error."""
    if exit_code == 0:
        return ExitCodeInterpretation(is_error=False)
    if exit_code == 1:
        return ExitCodeInterpretation(is_error=False, message="No matches found")
    return ExitCodeInterpretation(is_error=True, message=f"Search error (exit code {exit_code})")


def _diff_semantic(exit_code: int) -> ExitCodeInterpretation:
    """diff: 0=identical, 1=differences found, 2+=error."""
    if exit_code == 0:
        return ExitCodeInterpretation(is_error=False)
    if exit_code == 1:
        return ExitCodeInterpretation(is_error=False, message="Files differ")
    return ExitCodeInterpretation(is_error=True, message=f"Diff error (exit code {exit_code})")


def _find_semantic(exit_code: int) -> ExitCodeInterpretation:
    """find: 0=success, 1=some dirs inaccessible, 2+=error."""
    if exit_code == 0:
        return ExitCodeInterpretation(is_error=False)
    if exit_code == 1:
        return ExitCodeInterpretation(is_error=False, message="Some directories were inaccessible")
    return ExitCodeInterpretation(is_error=True, message=f"Find error (exit code {exit_code})")


def _test_semantic(exit_code: int) -> ExitCodeInterpretation:
    """test/[: 0=condition true, 1=condition false, 2+=error."""
    if exit_code == 0:
        return ExitCodeInterpretation(is_error=False)
    if exit_code == 1:
        return ExitCodeInterpretation(is_error=False, message="Condition is false")
    return ExitCodeInterpretation(is_error=True, message=f"Test error (exit code {exit_code})")


def _default_semantic(exit_code: int) -> ExitCodeInterpretation:
    """Default: treat any non-zero as error."""
    if exit_code == 0:
        return ExitCodeInterpretation(is_error=False)
    return ExitCodeInterpretation(is_error=True, message=f"Command failed with exit code {exit_code}")


# Command name → semantic function
_COMMAND_SEMANTICS = {
    "grep": _grep_semantic,
    "egrep": _grep_semantic,
    "fgrep": _grep_semantic,
    "rg": _grep_semantic,
    "ag": _grep_semantic,
    "ack": _grep_semantic,
    "diff": _diff_semantic,
    "find": _find_semantic,
    "test": _test_semantic,
    "[": _test_semantic,
}

# Commands that typically produce no stdout on success
SILENT_COMMANDS = frozenset({
    "mv", "cp", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp",
    "touch", "ln", "cd", "export", "unset", "wait",
})

# Commands classified as search/read operations
SEARCH_COMMANDS = frozenset({
    "find", "grep", "rg", "ag", "ack", "locate", "which", "whereis",
})

READ_COMMANDS = frozenset({
    "cat", "head", "tail", "less", "more",
    "wc", "stat", "file", "strings",
    "jq", "awk", "cut", "sort", "uniq", "tr",
})

LIST_COMMANDS = frozenset({
    "ls", "tree", "du",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _extract_last_command_name(command: str) -> str:
    """Extract the base command name from the last segment of a pipeline.

    For pipelines, the last command determines the exit code.
    """
    # Split by pipe/semicolon/and/or operators to get segments
    segments = re.split(r'\s*(?:\|{1,2}|&&|;)\s*', command.strip())
    last_segment = segments[-1].strip() if segments else command.strip()

    # Strip leading env var assignments
    stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', last_segment).strip()

    # Get the first word (command name)
    first_word = stripped.split()[0] if stripped.split() else ""

    # Normalize: /usr/bin/grep → grep
    return first_word.rsplit('/', 1)[-1] if '/' in first_word else first_word


def interpret_exit_code(command: str, exit_code: int) -> ExitCodeInterpretation:
    """Interpret a command's exit code with semantic awareness.

    Args:
        command: The full shell command string.
        exit_code: The process exit code.

    Returns:
        ExitCodeInterpretation with is_error flag and optional message.
    """
    if exit_code == 0:
        return ExitCodeInterpretation(is_error=False)

    base_cmd = _extract_last_command_name(command)
    semantic_fn = _COMMAND_SEMANTICS.get(base_cmd, _default_semantic)
    return semantic_fn(exit_code)


def is_silent_command(command: str) -> bool:
    """Check if a command typically produces no stdout on success."""
    if not command or not command.strip():
        return False
    segments = re.split(r'\s*(?:\|{1,2}|&&|;)\s*', command.strip())
    if not segments:
        return False

    for segment in segments:
        stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', segment.strip()).strip()
        first_word = stripped.split()[0] if stripped.split() else ""
        base_name = first_word.rsplit('/', 1)[-1] if '/' in first_word else first_word
        if base_name and base_name not in SILENT_COMMANDS:
            return False

    return bool(segments)


def is_search_or_read_command(command: str) -> bool:
    """Check if a command is purely a search/read operation."""
    segments = re.split(r'\s*(?:\|{1,2}|&&|;)\s*', command.strip())
    if not segments:
        return False

    neutral = {"echo", "printf", "true", "false", ":"}

    for segment in segments:
        stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', segment.strip()).strip()
        first_word = stripped.split()[0] if stripped.split() else ""
        base_name = first_word.rsplit('/', 1)[-1] if '/' in first_word else first_word

        if base_name in neutral:
            continue
        if base_name not in SEARCH_COMMANDS and base_name not in READ_COMMANDS and base_name not in LIST_COMMANDS:
            return False

    return True
