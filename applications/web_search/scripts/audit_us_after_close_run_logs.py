from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


SEARCH_CALL_RE = re.compile(r"Calling tool: 'mcp__AnySearch__batch_search'")
EXTRACT_CALL_RE = re.compile(r"Calling tool: 'mcp__AnySearch__extract'")
WRITE_CALL_RE = re.compile(r"Calling tool: 'write_markdown_file_raw'")
TOOL_CALL_RE = re.compile(r"Calling tool: '([^']+)'")
MAX_RESULTS_RE = re.compile(r"'max_results':\s*(\d+)")
QUOTE_HOST_PATTERNS = ("exa.ai",)
QUOTE_PATH_PATTERNS = ("/markets/stock/", "/library/markets/stock/")
MAX_EFFECTIVE_EXTRACTS = 5


@dataclass
class LogIssue:
    severity: str
    message: str


@dataclass
class LogAudit:
    path: str
    batch_search_calls: int
    planned_search_results: int
    extract_calls: int
    quote_extract_calls: int
    issues: list[LogIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


def _is_quote_url(url_or_block: str) -> bool:
    parsed = urlparse(url_or_block)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not host:
        compact = re.sub(r"\s+", "", url_or_block.lower())
        return any(pattern in compact for pattern in QUOTE_HOST_PATTERNS) and any(
            pattern in compact for pattern in QUOTE_PATH_PATTERNS
        )
    return any(pattern in host for pattern in QUOTE_HOST_PATTERNS) and any(pattern in path for pattern in QUOTE_PATH_PATTERNS)


def _extract_call_blocks(text: str) -> list[str]:
    return [block for event, _position, block, blocked in _tool_event_blocks(text) if event == "extract" and not blocked]


def _tool_event_blocks(text: str) -> list[tuple[str, int, str, bool]]:
    matches = list(TOOL_CALL_RE.finditer(text))
    events: list[tuple[str, int, str, bool]] = []
    for index, match in enumerate(matches):
        tool = match.group(1)
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[start:end]
        blocked = "Hook blocked action" in block
        if tool == "mcp__AnySearch__batch_search":
            events.append(("search", start, block, blocked))
        elif tool == "mcp__AnySearch__extract":
            events.append(("extract", start, block, blocked))
        elif tool == "write_markdown_file_raw":
            events.append(("write", start, block, blocked))
    return events


def _tool_events(text: str) -> list[tuple[str, int]]:
    return [(event, position) for event, position, _block, blocked in _tool_event_blocks(text) if not blocked]


def audit_log(path: Path) -> LogAudit:
    text = path.read_text(encoding="utf-8", errors="replace")
    event_blocks = _tool_event_blocks(text)
    search_blocks = [block for event, _position, block, blocked in event_blocks if event == "search" and not blocked]
    blocked_attempts = sum(1 for _event, _position, _block, blocked in event_blocks if blocked)
    batch_search_calls = len(search_blocks)
    planned_search_results = sum(int(value) for block in search_blocks for value in MAX_RESULTS_RE.findall(block))
    extract_blocks = _extract_call_blocks(text)
    quote_extract_calls = sum(1 for block in extract_blocks if _is_quote_url(block[:900]))
    issues: list[LogIssue] = []

    if batch_search_calls < 2:
        issues.append(LogIssue("error", "must run at least MarketDiscovery and DriverDiscovery batch_search"))
    if planned_search_results < 64:
        issues.append(LogIssue("error", f"planned search coverage too small: {planned_search_results}"))
    if "too many queries" in text or "batch_search 每次最多" in text:
        issues.append(LogIssue("error", "batch_search query count exceeded tool limit"))
    if blocked_attempts:
        issues.append(LogIssue("warning", f"blocked tool attempts ignored in effective sequence: {blocked_attempts}"))
    if quote_extract_calls:
        issues.append(LogIssue("warning", f"should not extract quote pages: {quote_extract_calls}"))
    if extract_blocks and len(extract_blocks) > MAX_EFFECTIVE_EXTRACTS:
        issues.append(LogIssue("warning", f"extract count exceeds workflow soft cap: {len(extract_blocks)}"))

    events = _tool_events(text)
    search_count_before_extract = 0
    for event, _position in events:
        if event == "search":
            search_count_before_extract += 1
        elif event == "extract":
            if search_count_before_extract < 2:
                issues.append(
                    LogIssue("warning", "should complete MarketDiscovery and DriverDiscovery before extract")
                )
            break

    extract_seen = 0
    for index, (event, _position) in enumerate(events):
        if event != "extract":
            continue
        extract_seen += 1
        if extract_seen == MAX_EFFECTIVE_EXTRACTS:
            next_tool = events[index + 1][0] if index + 1 < len(events) else None
            if next_tool != "write":
                issues.append(LogIssue("warning", f"should write report after extract_count reaches {MAX_EFFECTIVE_EXTRACTS}"))
            break

    return LogAudit(
        path=str(path),
        batch_search_calls=batch_search_calls,
        planned_search_results=planned_search_results,
        extract_calls=len(extract_blocks),
        quote_extract_calls=quote_extract_calls,
        issues=issues,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    audits = [audit_log(path) for path in args.paths]
    payload = [{**asdict(audit), "ok": audit.ok, "issues": [asdict(issue) for issue in audit.issues]} for audit in audits]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for audit in audits:
            status = "PASS" if audit.ok else "FAIL"
            print(
                f"{status} {audit.path} batch={audit.batch_search_calls} "
                f"planned_results={audit.planned_search_results} extract={audit.extract_calls}"
            )
            for issue in audit.issues:
                print(f"  [{issue.severity}] {issue.message}")

    return 0 if all(audit.ok for audit in audits) else 1


if __name__ == "__main__":
    raise SystemExit(main())
