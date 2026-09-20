"""Shell command exit code semantic interpretation.

Many commands use non-zero exit codes to convey information
beyond simple success/failure.  For example, ``grep`` returns 1
when no matches are found — this is not an error condition.

This module provides contextual interpretation so the LLM
does not misinterpret informational exit codes as failures.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExitCodeInterpretation:
    """Semantic interpretation of a command's exit code."""
    is_error: bool
    message: str | None = None


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

def _heredoc_declarations(line: str) -> list[tuple[str, bool]]:
    """Return heredoc delimiters declared outside quotes/comments on one line."""
    declarations: list[tuple[str, bool]] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "#" and (
            index == 0
            or line[index - 1].isspace()
            or line[index - 1] in {";", "|", "&", "("}
        ):
            break
        if not line.startswith("<<", index) or line.startswith("<<<", index):
            index += 1
            continue

        cursor = index + 2
        strip_tabs = cursor < len(line) and line[cursor] == "-"
        if strip_tabs:
            cursor += 1
        while cursor < len(line) and line[cursor] in {" ", "\t"}:
            cursor += 1
        if cursor >= len(line):
            break

        delimiter_chars: list[str] = []
        while cursor < len(line) and not line[cursor].isspace() and line[cursor] not in ";|&()<>":
            char = line[cursor]
            if char in {"'", '"'}:
                end = line.find(char, cursor + 1)
                if end < 0:
                    cursor = len(line)
                    break
                delimiter_chars.append(line[cursor + 1 : end])
                cursor = end + 1
                continue
            if char == "\\" and cursor + 1 < len(line):
                cursor += 1
                delimiter_chars.append(line[cursor])
                cursor += 1
                continue
            delimiter_chars.append(char)
            cursor += 1
        delimiter = "".join(delimiter_chars)
        if delimiter:
            declarations.append((delimiter, strip_tabs))
        index = max(cursor, index + 2)
    return declarations


def _consume_logical_shell_line(command: str, start: int) -> tuple[str, int]:
    """Read one shell command line, folding unquoted backslash-newlines."""
    output: list[str] = []
    quote: str | None = None
    index = start
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            if command.startswith("\r\n", index + 1):
                index += 3
                continue
            if index + 1 < len(command) and command[index + 1] == "\n":
                index += 2
                continue
            output.append(char)
            if index + 1 < len(command):
                output.append(command[index + 1])
                index += 2
            else:
                index += 1
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        output.append(char)
        index += 1
        if char == "\n" and quote is None:
            break
    return "".join(output), index


def _consume_physical_line(command: str, start: int) -> tuple[str, int]:
    newline = command.find("\n", start)
    if newline < 0:
        return command[start:], len(command)
    return command[start : newline + 1], newline + 1


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc data so its text is never parsed as shell syntax."""
    output: list[str] = []
    pending: list[tuple[str, bool]] = []
    needs_separator = False
    cursor = 0
    while cursor < len(command):
        if pending:
            raw_line, cursor = _consume_physical_line(command, cursor)
            delimiter, strip_tabs = pending[0]
            candidate = raw_line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delimiter:
                pending.pop(0)
                if not pending:
                    needs_separator = True
            continue

        logical_line, cursor = _consume_logical_shell_line(command, cursor)
        if needs_separator:
            output.append("\n")
            needs_separator = False

        declarations = _heredoc_declarations(logical_line)
        if declarations:
            output.append(logical_line.rstrip("\r\n"))
            pending.extend(declarations)
        else:
            output.append(logical_line)

    return "".join(output)


def _scan_unquoted_operators(
    command: str,
    *,
    strip_heredocs: bool = True,
) -> list[tuple[int, int, str]]:
    """Locate shell control operators while ignoring quoted/escaped text."""
    if strip_heredocs:
        command = _strip_heredoc_bodies(command)

    def next_executable_index(start: int) -> int | None:
        """Skip blank and comment-only lines after a command separator."""
        cursor = start
        while cursor < len(command):
            while cursor < len(command) and command[cursor].isspace():
                cursor += 1
            if cursor >= len(command):
                return None
            if command[cursor] != "#":
                return cursor
            newline = command.find("\n", cursor + 1)
            if newline < 0:
                return None
            cursor = newline + 1
        return None

    operators: list[tuple[int, int, str]] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "#" and (
            index == 0
            or command[index - 1].isspace()
            or command[index - 1] in {";", "|", "&", "("}
        ):
            newline = command.find("\n", index + 1)
            if newline < 0:
                break
            next_index = next_executable_index(newline + 1)
            if next_index is None:
                break
            operators.append((newline, next_index, ";"))
            index = next_index
            continue
        if char == "\n":
            next_index = next_executable_index(index + 1)
            if next_index is None:
                break
            operators.append((index, next_index, ";"))
            index = next_index
            continue
        if command.startswith("&&", index) or command.startswith("||", index):
            operators.append((index, index + 2, command[index : index + 2]))
            index += 2
            continue
        if char in {";", "|"}:
            operators.append((index, index + 1, char))
        index += 1
    return operators


def _split_unquoted_control_segments(command: str) -> list[str]:
    command = _strip_heredoc_bodies(command)
    segments: list[str] = []
    start = 0
    for operator_start, operator_end, _operator in _scan_unquoted_operators(
        command,
        strip_heredocs=False,
    ):
        segments.append(command[start:operator_start].strip())
        start = operator_end
    segments.append(command[start:].strip())
    return segments


def _has_unquoted_list_operator(command: str) -> bool:
    """Return whether ``&&``, ``||`` or ``;`` occurs outside shell quotes."""
    return any(operator in {"&&", "||", ";"} for _, _, operator in _scan_unquoted_operators(command))

def _extract_last_command_name(command: str) -> str:
    """Extract the base command name from the last segment of a pipeline.

    For pipelines, the last command determines the exit code.
    """
    # Split only on actual shell operators, not operator-like text in quotes.
    segments = _split_unquoted_control_segments(command.strip())
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

    # A short-circuit/list operator means the shell's final status may have
    # come from an earlier segment that prevented the apparent final command
    # from running. Without per-segment statuses, non-zero is conservatively
    # an error. Pipelines remain safe because their status is the last command.
    if _has_unquoted_list_operator(command):
        return _default_semantic(exit_code)

    base_cmd = _extract_last_command_name(command)
    semantic_fn = _COMMAND_SEMANTICS.get(base_cmd, _default_semantic)
    return semantic_fn(exit_code)


def is_silent_command(command: str) -> bool:
    """Check if a command typically produces no stdout on success."""
    if not command or not command.strip():
        return False
    segments = _split_unquoted_control_segments(command.strip())
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
    segments = _split_unquoted_control_segments(command.strip())
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
