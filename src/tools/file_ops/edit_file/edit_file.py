"""
EditFile tool implementation.

Performs string replacement in files using a four-level matching strategy:
exact → quote-normalized → whitespace-tolerant → token-based.
Includes staleness detection via ReadFileState.
"""

from __future__ import annotations

from pathlib import Path

from src.lib.logging import get_logger

from .._safety import MAX_EDIT_FILE_SIZE, normalize_path, validate_file_access
from .._read_file_state import get_read_file_state
from .utils import (
    build_token_regex,
    build_whitespace_tolerant_regex,
    count_occurrences,
    detect_line_ending,
    find_actual_string,
    find_similar_lines,
    normalize_quotes,
    normalize_to_lf,
    preserve_quote_style,
    restore_line_ending,
)

logger = get_logger(__name__)


def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Performs string replacement in files.

    The old_string must be unique in the file unless replace_all is True.
    When editing text from read_file output, preserve the exact indentation
    (tabs/spaces) as it appears AFTER the line-number prefix.
    Always prefer editing existing files in the codebase over writing new files.
    The edit will fail if old_string is not unique — provide more surrounding
    context to make it unique, or use replace_all=True to change every instance.
    Use replace_all for renaming variables or strings across the file.
    If old_string is empty and the file does not exist, a new file is created.

    Matching strategy (tried in order):
    1. Exact literal match
    2. Quote-normalized match (curly quotes ↔ straight quotes)
    3. Whitespace-tolerant regex (handles indentation changes)
    4. Token-based regex (ignores all whitespace)

    Args:
        file_path: Absolute path to the file to edit.
        old_string: The text to find and replace.  If empty and the file
            does not exist, a new file is created with *new_string*.
        new_string: The replacement text.
        replace_all: If ``True``, replace **all** occurrences instead
            of requiring a unique match.  Default ``False``.

    Returns:
        A success message describing what was done, or an error message
        explaining why the edit failed.

    Examples:
        >>> edit_file("/tmp/app.py", "old_func()", "new_func()")
        'Successfully edited /tmp/app.py'

        >>> edit_file("/tmp/new.py", "", "print('hello')")
        'Successfully created new file: /tmp/new.py'

        >>> edit_file("/tmp/app.py", "x", "y", replace_all=True)
        'Successfully edited /tmp/app.py (replaced all occurrences)'
    """
    # -- Sanitize path whitespace ------------------------------------------
    if file_path:
        file_path = file_path.strip()

    # -- Path access control -----------------------------------------------
    validate_file_access(file_path, "write", tool_name="edit_file")

    path_obj = normalize_path(file_path)

    # -- New-file creation -------------------------------------------------
    if not path_obj.exists():
        if not old_string:
            try:
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                path_obj.write_text(new_string, encoding="utf-8")
                # Update state cache
                state = get_read_file_state()
                state.update_after_write(path_obj, new_string)
                return f"Successfully created new file: {file_path}"
            except Exception as exc:
                return f"Error creating file {file_path}: {exc}"
        else:
            return f"File does not exist: {file_path}"

    # -- Existing file, empty old_string -----------------------------------
    if not old_string:
        return (
            f"File already exists: {file_path}\n"
            "To overwrite the file, use write_file instead.\n"
            "To edit the file, provide the old_string segment to replace."
        )

    # -- Same-string guard -------------------------------------------------
    if old_string == new_string:
        return "No changes needed: old_string and new_string are identical."

    # -- Staleness check ---------------------------------------------------
    state = get_read_file_state()
    stale_msg = state.check_staleness(path_obj)
    if stale_msg is not None:
        return stale_msg

    # -- Size guard --------------------------------------------------------
    try:
        file_size = path_obj.stat().st_size
    except OSError as exc:
        return f"Cannot stat file '{file_path}': {exc}"

    if file_size > MAX_EDIT_FILE_SIZE:
        return (
            f"File '{file_path}' is too large to edit "
            f"({file_size / (1024 * 1024):.1f} MB)."
        )

    # -- Read file ---------------------------------------------------------
    try:
        raw_bytes = path_obj.read_bytes()
        content = raw_bytes.decode("utf-8")
    except Exception as exc:
        return f"Failed to read file {file_path}: {exc}"

    original_eol = detect_line_ending(content)
    content_lf = normalize_to_lf(content)
    old_lf = normalize_to_lf(old_string)
    new_lf = normalize_to_lf(new_string)

    # -- Four-level matching -----------------------------------------------
    expected = None if replace_all else 1

    # Strategy 1: Exact literal
    count_exact = count_occurrences(content_lf, old_lf)

    # Strategy 2: Quote-normalized
    actual_old = find_actual_string(content_lf, old_lf)
    count_quote = 0
    if actual_old is not None and actual_old != old_lf:
        count_quote = count_occurrences(content_lf, actual_old)

    # Strategy 3: Whitespace-tolerant regex
    ws_regex = build_whitespace_tolerant_regex(old_lf)
    count_ws = len(ws_regex.findall(content_lf))

    # Strategy 4: Token-based regex
    token_regex = build_token_regex(old_lf)
    count_token = len(token_regex.findall(content_lf))

    new_content_lf = content_lf
    replacement_done = False
    strategy_name = ""

    def _do_replace(count: int, label: str) -> bool:
        """Attempt replacement if count meets expectation."""
        nonlocal new_content_lf, replacement_done, strategy_name
        if expected is not None and count != expected:
            return False
        if count == 0:
            return False
        return True

    # Apply best matching strategy
    if expected is None:
        # replace_all mode: prefer exact, then quote, then ws, then token
        if count_exact > 0:
            new_content_lf = content_lf.replace(old_lf, new_lf)
            replacement_done = True
            strategy_name = "exact"
        elif actual_old is not None and actual_old != old_lf and count_quote > 0:
            actual_new = preserve_quote_style(old_lf, actual_old, new_lf)
            new_content_lf = content_lf.replace(actual_old, actual_new)
            replacement_done = True
            strategy_name = "quote-normalized"
        elif count_ws > 0:
            new_content_lf = ws_regex.sub(lambda m: new_lf, content_lf)
            replacement_done = True
            strategy_name = "whitespace-tolerant"
        elif count_token > 0:
            new_content_lf = token_regex.sub(lambda m: new_lf, content_lf)
            replacement_done = True
            strategy_name = "token-based"
    else:
        # Single-replacement mode: need exactly 1 match
        if count_exact == 1:
            new_content_lf = content_lf.replace(old_lf, new_lf, 1)
            replacement_done = True
            strategy_name = "exact"
        elif actual_old is not None and actual_old != old_lf and count_quote == 1:
            actual_new = preserve_quote_style(old_lf, actual_old, new_lf)
            new_content_lf = content_lf.replace(actual_old, actual_new, 1)
            replacement_done = True
            strategy_name = "quote-normalized"
        elif count_ws == 1:
            new_content_lf = ws_regex.sub(lambda m: new_lf, content_lf, count=1)
            replacement_done = True
            strategy_name = "whitespace-tolerant"
        elif count_token == 1:
            new_content_lf = token_regex.sub(lambda m: new_lf, content_lf, count=1)
            replacement_done = True
            strategy_name = "token-based"

    # -- Handle match failures ---------------------------------------------
    if not replacement_done:
        all_zero = (count_exact == 0 and count_ws == 0 and count_token == 0
                    and (actual_old is None or count_quote == 0))
        if all_zero:
            hint = find_similar_lines(content_lf, old_lf)
            msg = (
                f"Could not find old_string in {file_path}\n"
                "Tried exact, quote-normalized, whitespace-flexible, "
                "and token-based matching."
            )
            if hint:
                msg += f"\n\nDid you mean (most similar match)?\n{hint}"
            msg += (
                "\n\nSuggestions:\n"
                "- Use read_file to check the actual file contents.\n"
                "- Ensure old_string matches the file exactly."
            )
            return msg

        # Multiple matches — guide towards replace_all
        return (
            f"Multiple matches found in {file_path}. "
            f"old_string is not unique.\n"
            f"- {count_exact} exact | {count_ws} whitespace-tolerant | "
            f"{count_token} token-based matches\n"
            "Either provide more surrounding context to make old_string "
            "unique, or use replace_all=True."
        )

    # -- Detect no-op ------------------------------------------------------
    final_content = restore_line_ending(new_content_lf, original_eol)
    if final_content == content:
        return f"File content unchanged for {file_path}."

    # -- Write (use bytes to preserve CRLF) --------------------------------
    try:
        path_obj.write_bytes(final_content.encode("utf-8"))
    except Exception as exc:
        return f"Failed to write file {file_path}: {exc}"

    # -- Update state cache ------------------------------------------------
    state.update_after_write(path_obj, final_content)

    suffix = ""
    if replace_all:
        suffix = " (replaced all occurrences)"
    if strategy_name and strategy_name != "exact":
        suffix += f" [matched via {strategy_name}]"

    logger.debug("edit_file %s strategy=%s replace_all=%s", file_path, strategy_name, replace_all)
    return f"Successfully edited {file_path}{suffix}"
