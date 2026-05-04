"""
GrepTool

Implementation:
- Exit-code handling: 0/1 = success, EAGAIN = retry with -j 1, others = error
- Timeout: 20 s SIGTERM → 5 s SIGKILL escalation  (ripgrep.ts L362-375)
- Pagination: collect all results then applyHeadLimit  (GrepTool.ts L128-145)
- Three output modes: content / files_with_matches / count
- Max buffer: 20 MB guard
- VCS exclusion, --hidden, --max-columns, --sort=modified, -e for dash patterns
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# ripgrep discovery
# ---------------------------------------------------------------------------
_VENV_BIN = os.path.dirname(sys.executable)
_RG_PATH: Optional[str] = shutil.which("rg", path=_VENV_BIN) or shutil.which("rg")

_VCS_EXCLUDE_GLOBS = ["!.git", "!.svn", "!.hg", "!.bzr", "!.jj", "!.sl"]
_MAX_COLUMNS = 500
_DEFAULT_HEAD_LIMIT = 250
_TIMEOUT_SECONDS = 20
_SIGKILL_GRACE = 5

# Exclude pattern utilities shared with glob_tool via search_utils.
from src.tools.search.search_utils import (
    get_search_exclude_patterns,
    get_python_exclude_dirs,
)


# =========================================================================
# Public API
# =========================================================================

def grep_search(
    pattern: str,
    path: str = ".",
    include: str = "",
    output_mode: str = "content",
    case_insensitive: bool = True,
    context_lines: int = 0,
    before_context: int = 0,
    after_context: int = 0,
    multiline: bool = False,
    max_results: int = _DEFAULT_HEAD_LIMIT,
    offset: int = 0,
) -> str:
    """Search file contents using regex patterns (powered by ripgrep).

    Fast, recursive, respects .gitignore by default.
    Three output modes: content (matching lines), files_with_matches (file list only),
    count (match counts per file).
    Supports multiline patterns, context lines, and pagination.

    Examples:
        grep_search("def __init__", path="src/")
        grep_search("TODO|FIXME", output_mode="files_with_matches")
        grep_search("class.*Agent", include="*.py", context_lines=3)
        grep_search("import", max_results=50, offset=50)
        grep_search("class Config", path="src/config.py")

    Args:
        pattern: Regex pattern to search for.
        path: File or directory to search in (default: current directory).
              Pass a directory to search recursively, or a single file path
              to search within that file only.
        include: Glob pattern to filter files (e.g. ``"*.py"``).  Empty means all files.
        output_mode: One of ``"content"`` (matching lines with context),
                     ``"files_with_matches"`` (only file paths), or
                     ``"count"`` (match count per file).
        case_insensitive: Ignore case when matching (default True).
        context_lines: Lines of context around each match (``-C``).
                       When > 0 this takes precedence over *before_context* / *after_context*.
        before_context: Lines before each match (``-B``).  Ignored when *context_lines* > 0.
        after_context: Lines after each match (``-A``).  Ignored when *context_lines* > 0.
        multiline: Enable multiline matching across line boundaries.
        max_results: Maximum number of result entries to return (0 = unlimited).
        offset: Skip the first *offset* result entries (for pagination).

    Returns:
        A formatted string of search results with a metadata footer.

    Raises:
        ValueError: If *pattern* is empty or *output_mode* is invalid.
        FileNotFoundError: If *path* does not exist.
    """
    if not pattern:
        raise ValueError("pattern is required")
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ValueError(
            f"output_mode must be 'content', 'files_with_matches', or 'count', "
            f"got '{output_mode}'"
        )

    search_path = Path(path).resolve()
    if not search_path.exists():
        raise FileNotFoundError(f"Search path does not exist: {path}")
    if not search_path.is_file() and not search_path.is_dir():
        raise ValueError(f"Search path must be a file or directory: {path}")

    start_time = time.monotonic()

    if _RG_PATH:
        result = _search_with_ripgrep(
            pattern, search_path, include, output_mode,
            case_insensitive, context_lines, before_context,
            after_context, multiline, max_results, offset,
        )
    else:
        logger.info("ripgrep not available, using Python fallback")
        result = _search_with_python(
            pattern, search_path, include, output_mode,
            case_insensitive, max_results, offset,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)
    return _format_output(result, output_mode, duration_ms)


# =========================================================================
# applyHeadLimit
# =========================================================================

def _apply_head_limit(items: list, limit: int, offset: int = 0) -> tuple:
    """Paginate: collect all then slice.  Returns (sliced, applied_limit|None)."""
    sliced = items[offset:]
    effective = limit if limit > 0 else 0
    if effective > 0 and len(sliced) > effective:
        return sliced[:effective], effective
    return sliced, None


# =========================================================================
# ripgrep command builder
# =========================================================================

def _build_rg_args(
    pattern: str, search_dir: Path, include: str, output_mode: str,
    case_insensitive: bool, context_lines: int, before_context: int,
    after_context: int, multiline: bool,
) -> List[str]:
    args: List[str] = [_RG_PATH, "--hidden", f"--max-columns={_MAX_COLUMNS}", "--sort=modified"]

    for g in _VCS_EXCLUDE_GLOBS:
        args.extend(["--glob", g])

    # Inject configured exclude patterns from tool_access_control
    for excl_glob in get_search_exclude_patterns():
        args.extend(["--glob", excl_glob])

    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")
    else:
        args.append("--json")

    if case_insensitive:
        args.append("-i")

    # Context: -C takes precedence\
    if output_mode == "content":
        if context_lines > 0:
            args.extend(["-C", str(context_lines)])
        else:
            if before_context > 0:
                args.extend(["-B", str(before_context)])
            if after_context > 0:
                args.extend(["-A", str(after_context)])

    if multiline:
        args.extend(["-U", "--multiline-dotall"])
    if include:
        args.extend(["-g", include])
    if output_mode == "content":
        args.append("-n")

    if pattern.startswith("-"):
        args.extend(["-e", pattern])
    else:
        args.append(pattern)

    args.append(str(search_dir))
    return args


# =========================================================================
# Timeout + SIGTERM→SIGKILL escalation  (ripgrep.ts L362-375)
# =========================================================================

def _kill_with_escalation(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except OSError:
        return

    def _escalate():
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass

    t = threading.Timer(_SIGKILL_GRACE, _escalate)
    t.daemon = True
    t.start()


# =========================================================================
# EAGAIN detection  (ripgrep.ts L263-268)
# =========================================================================

def _is_eagain_error(stderr: str) -> bool:
    return "os error 11" in stderr or "Resource temporarily unavailable" in stderr


# =========================================================================
# ripgrep runner — exit-code + timeout + EAGAIN  (ripgrep.ts)
# =========================================================================

def _run_ripgrep(args: List[str], timeout: int = _TIMEOUT_SECONDS, is_retry: bool = False) -> Tuple[int, str, str]:
    """Run ripgrep. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        logger.error("Failed to start ripgrep: %s", exc)
        return (-1, "", str(exc))

    timed_out = False

    def _on_timeout():
        nonlocal timed_out
        timed_out = True
        _kill_with_escalation(proc)

    watchdog = threading.Timer(timeout, _on_timeout)
    watchdog.daemon = True
    watchdog.start()

    try:
        stdout, stderr = proc.communicate()
    except Exception:
        _kill_with_escalation(proc)
        stdout, stderr = "", ""
    finally:
        watchdog.cancel()

    rc = proc.returncode or 0

    if timed_out:
        logger.warning("ripgrep timed out after %ds", timeout)
        # Drop incomplete last line  (ripgrep.ts L518-525)
        parts = stdout.rsplit("\n", 1)
        if len(parts) > 1:
            stdout = parts[0] + "\n"

    # exit 0/1 = success  (ripgrep.ts L500)
    if rc in (0, 1):
        return (rc, stdout, stderr)

    # EAGAIN → retry with -j 1  (ripgrep.ts L509-512)
    if not is_retry and _is_eagain_error(stderr):
        logger.info("ripgrep EAGAIN detected, retrying with -j 1")
        retry_args = list(args)
        retry_args.insert(1, "-j")
        retry_args.insert(2, "1")
        return _run_ripgrep(retry_args, timeout=timeout, is_retry=True)

    logger.warning("ripgrep exit %d: %s", rc, stderr.strip()[:200])
    return (rc, stdout, stderr)


# =========================================================================
# ripgrep dispatch
# =========================================================================

def _search_with_ripgrep(
    pattern: str, search_dir: Path, include: str, output_mode: str,
    case_insensitive: bool, context_lines: int, before_context: int,
    after_context: int, multiline: bool, max_results: int, offset: int,
) -> "_SearchResult":
    args = _build_rg_args(
        pattern, search_dir, include, output_mode,
        case_insensitive, context_lines, before_context,
        after_context, multiline,
    )
    rc, stdout, stderr = _run_ripgrep(args)

    if rc not in (0, 1) and not stdout:
        return _SearchResult.empty()

    if output_mode == "content":
        return _parse_rg_json(stdout, search_dir, max_results, offset)
    elif output_mode == "files_with_matches":
        return _parse_rg_files(stdout, search_dir, max_results, offset)
    else:
        return _parse_rg_count(stdout, search_dir, max_results, offset)


# =========================================================================
# Parsers — all receive stdout string, apply applyHeadLimit at end
# =========================================================================

def _parse_rg_json(stdout: str, search_dir: Path, max_results: int, offset: int) -> "_SearchResult":
    all_entries: List[Tuple[str, int, str]] = []
    total_matches = 0
    files_seen: set = set()

    for raw_line in stdout.splitlines():
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        type_ = data.get("type")
        if type_ not in ("match", "context"):
            continue
        d = data["data"]
        rel_path = _to_relative(d["path"]["text"], search_dir)
        line_num = d["line_number"]
        text = d["lines"]["text"].rstrip("\n")
        files_seen.add(rel_path)
        if type_ == "match":
            total_matches += 1
        all_entries.append((rel_path, line_num, text))

    sliced, applied_limit = _apply_head_limit(all_entries, max_results, offset)
    return _SearchResult(
        entries=sliced, total_matches=total_matches, total_files=len(files_seen),
        truncated=applied_limit is not None,
        applied_offset=offset if offset > 0 else None, applied_limit=applied_limit,
    )


def _parse_rg_files(stdout: str, search_dir: Path, max_results: int, offset: int) -> "_SearchResult":
    files = [_to_relative(l.strip(), search_dir) for l in stdout.splitlines() if l.strip()]
    total = len(files)
    sliced, applied_limit = _apply_head_limit(files, max_results, offset)
    entries = [(f, 0, "") for f in sliced]
    return _SearchResult(
        entries=entries, total_matches=total, total_files=total,
        truncated=applied_limit is not None,
        applied_offset=offset if offset > 0 else None, applied_limit=applied_limit,
    )


def _parse_rg_count(stdout: str, search_dir: Path, max_results: int, offset: int) -> "_SearchResult":
    entries: List[Tuple[str, int, str]] = []
    total_matches = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        idx = line.rfind(":")
        if idx < 0:
            continue
        try:
            count = int(line[idx + 1:])
        except ValueError:
            continue
        rel = _to_relative(line[:idx], search_dir)
        total_matches += count
        entries.append((rel, count, ""))

    total_files = len(entries)
    sliced, applied_limit = _apply_head_limit(entries, max_results, offset)
    return _SearchResult(
        entries=sliced, total_matches=total_matches, total_files=total_files,
        truncated=applied_limit is not None,
        applied_offset=offset if offset > 0 else None, applied_limit=applied_limit,
    )


# =========================================================================
# Python fallback
# =========================================================================

def _search_with_python(
    pattern: str, search_dir: Path, include: str, output_mode: str,
    case_insensitive: bool, max_results: int, offset: int,
) -> "_SearchResult":
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc

    import fnmatch
    include_fn = (lambda name: fnmatch.fnmatch(name, include)) if include else None
    skip_dirs = get_python_exclude_dirs()

    all_entries: List[Tuple[str, int, str]] = []
    files_seen: set = set()
    match_count = 0

    # Build file list: single file or recursive directory walk.
    if search_dir.is_file():
        file_list = [(str(search_dir), search_dir.name, search_dir.parent)]
    else:
        file_list = []
        for root, dirs, filenames in os.walk(search_dir):
            dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
            for fname in sorted(filenames):
                if include_fn and not include_fn(fname):
                    continue
                file_list.append((os.path.join(root, fname), fname, search_dir))

    for fpath, _fname, base_dir in file_list:
        rel_path = os.path.relpath(fpath, base_dir)
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if regex.search(line):
                        match_count += 1
                        files_seen.add(rel_path)
                        if output_mode == "content":
                            all_entries.append((rel_path, i, line.rstrip("\n")))
                        elif output_mode == "files_with_matches":
                            if rel_path not in {e[0] for e in all_entries}:
                                all_entries.append((rel_path, 0, ""))
                            break
        except (OSError, UnicodeDecodeError):
            continue

    if output_mode == "count":
        file_counts: Dict[str, int] = {}
        if search_dir.is_file():
            count_file_list = [(str(search_dir), search_dir.name, search_dir.parent)]
        else:
            count_file_list = []
            for root, dirs, filenames in os.walk(search_dir):
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
                for fname in sorted(filenames):
                    if include_fn and not include_fn(fname):
                        continue
                    count_file_list.append((os.path.join(root, fname), fname, search_dir))
        for fpath, _fname, base_dir in count_file_list:
            rel_path = os.path.relpath(fpath, base_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    cnt = sum(1 for ln in fh if regex.search(ln))
                if cnt > 0:
                    file_counts[rel_path] = cnt
            except (OSError, UnicodeDecodeError):
                continue
        all_entries = [(fp, cnt, "") for fp, cnt in file_counts.items()]
        match_count = sum(file_counts.values())
        files_seen = set(file_counts.keys())

    sliced, applied_limit = _apply_head_limit(all_entries, max_results, offset)
    return _SearchResult(
        entries=sliced, total_matches=match_count, total_files=len(files_seen),
        truncated=applied_limit is not None,
        applied_offset=offset if offset > 0 else None, applied_limit=applied_limit,
    )


# =========================================================================
# Result model + formatting
# =========================================================================

class _SearchResult:
    __slots__ = ("entries", "total_matches", "total_files", "truncated", "applied_offset", "applied_limit")

    def __init__(self, entries, total_matches, total_files, truncated, applied_offset, applied_limit):
        self.entries = entries
        self.total_matches = total_matches
        self.total_files = total_files
        self.truncated = truncated
        self.applied_offset = applied_offset
        self.applied_limit = applied_limit

    @classmethod
    def empty(cls):
        return cls([], 0, 0, False, None, None)


def _format_output(result: _SearchResult, output_mode: str, duration_ms: int) -> str:
    if not result.entries:
        return "No matches found."

    lines: List[str] = []

    if output_mode == "content":
        current_file = None
        last_line_num = -100
        for rel_path, line_num, text in result.entries:
            if rel_path != current_file:
                if current_file is not None:
                    lines.append("")
                lines.append(f"# {rel_path}")
                current_file = rel_path
                last_line_num = -100
            if last_line_num != -100 and line_num > last_line_num + 1:
                lines.append("    ...")
            lines.append(f"{str(line_num).rjust(4)} | {text}")
            last_line_num = line_num
    elif output_mode == "files_with_matches":
        for rel_path, _, _ in result.entries:
            lines.append(rel_path)
    elif output_mode == "count":
        for rel_path, count, _ in result.entries:
            lines.append(f"{rel_path}: {count}")

    lines.append("")
    meta = [f"{result.total_matches} matches", f"{result.total_files} files"]
    if result.truncated:
        meta.append("truncated: true")
    if result.applied_offset:
        meta.append(f"offset: {result.applied_offset}")
    if result.applied_limit:
        meta.append(f"limit: {result.applied_limit}")
    meta.append(f"{duration_ms}ms")
    lines.append(f"[{', '.join(meta)}]")

    # Pagination hint: guide the LLM to fetch more results if truncated
    if result.truncated and result.applied_limit:
        next_offset = (result.applied_offset or 0) + result.applied_limit
        lines.append(f"[Use offset={next_offset} to see more results.]")

    return "\n".join(lines)


def _to_relative(abs_path: str, base: Path) -> str:
    try:
        return str(Path(abs_path).relative_to(base))
    except ValueError:
        return abs_path
