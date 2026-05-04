"""Pipe redirect normalization — prevent stdin-related hangs in pipelines.

When ``stdin`` is redirected to ``/dev/null`` (the default for
subprocess execution), piped commands like ``rg foo | wc -l`` can
hang because ``eval`` applies the redirect to the *last* command
rather than the *first*.

This module rearranges the command so that ``< /dev/null`` is
explicitly placed after the first command in a pipeline::

    rg foo | wc -l   →   rg foo < /dev/null | wc -l

A conservative skip-list ensures complex commands (``$()``,
backticks, control structures, etc.) are left untouched.

Design aligned with Claude Code's rearrangePipeCommand() in
bashPipeCommand.ts.
"""

import re
from typing import List, Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)

# Patterns that indicate the command is too complex to safely rearrange.
# When any of these are detected (in unquoted context), the command is
# returned unchanged.
_SKIP_PATTERNS = [
    re.compile(r"\$\("),           # $() command substitution
    re.compile(r"`"),              # backtick substitution
    re.compile(r"\$\{"),           # ${} parameter expansion
    re.compile(r"\$\["),           # $[] arithmetic expansion
    re.compile(r"<\("),            # <() process substitution
    re.compile(r">\("),            # >() process substitution
]

# Shell control-structure keywords.  If the command contains these
# at word boundaries, it likely has complex structure that we must
# not rearrange (the pipe may be inside a loop body or branch).
_CONTROL_KEYWORDS = re.compile(
    r"\b(?:for|while|until|if|case|select|do|done|then|fi|esac)\b"
)


def rearrange_pipe_command(command: str) -> str:
    """Move ``< /dev/null`` to apply to the first command in a pipeline.

    If the command contains a pipe (``|``) and none of the skip
    conditions apply, the returned command will have
    ``< /dev/null`` inserted after the first pipeline segment.

    If the command cannot be safely rearranged (complex syntax,
    no pipes, etc.), it is returned unchanged.

    Args:
        command: The raw shell command string.

    Returns:
        The rearranged command, or the original if no change is needed.
    """
    if not command or not command.strip():
        return command

    # Quick check — no pipe at all.
    if "|" not in command:
        return command

    # Skip if command contains real newlines (not continuation).
    if "\n" in command:
        return command

    # Extract unquoted content for pattern checking.
    unquoted = _extract_unquoted(command)

    # Skip if unquoted content contains complex patterns.
    for pattern in _SKIP_PATTERNS:
        if pattern.search(unquoted):
            return command

    # Skip if command contains control structures.
    if _CONTROL_KEYWORDS.search(unquoted):
        return command

    # Skip if command already has an explicit stdin redirect.
    if re.search(r"<\s*\S", unquoted):
        return command

    # Split on unquoted single pipe (not ||).
    segments = _split_on_unquoted_pipe(command)
    if segments is None or len(segments) < 2:
        return command

    # Insert < /dev/null after the first segment.
    first = segments[0].rstrip()
    rest = " | ".join(s.strip() for s in segments[1:])
    result = f"{first} < /dev/null | {rest}"
    logger.debug("Pipe redirect: %r → %r", command, result)
    return result


def _extract_unquoted(command: str) -> str:
    """Return only the unquoted parts of the command.

    Single-quoted and double-quoted regions are replaced with spaces
    so that patterns inside quotes do not trigger false positives.
    """
    result = []
    in_single = False
    in_double = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single:
                result.append(" ")
            continue

        if ch == "\\" and not in_single:
            escaped = True
            result.append(" ")
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            result.append(" ")
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            result.append(" ")
            continue

        if in_single or in_double:
            result.append(" ")
        else:
            result.append(ch)

    return "".join(result)


def _split_on_unquoted_pipe(command: str) -> Optional[List[str]]:
    """Split *command* on unquoted single ``|`` (not ``||``).

    Returns a list of pipe segments, or None if splitting fails.
    """
    segments: List[str] = []
    current: List[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0

    while i < len(command):
        ch = command[i]

        if escaped:
            escaped = False
            current.append(ch)
            i += 1
            continue

        if ch == "\\" and not in_single:
            escaped = True
            current.append(ch)
            i += 1
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        if ch == "|" and not in_single and not in_double:
            # Check if this is || (or-operator), not |.
            if i + 1 < len(command) and command[i + 1] == "|":
                current.append("||")
                i += 2
                continue
            # It's a single pipe — split here.
            segments.append("".join(current))
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    # Flush remainder.
    segments.append("".join(current))

    # If we're still inside quotes, the command is malformed — bail.
    if in_single or in_double:
        return None

    return segments
