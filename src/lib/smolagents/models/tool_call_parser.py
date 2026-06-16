"""Strict AgentLoom tool-call parser for smolagents model output.

The fallback parser is deliberately narrow. It accepts explicit tool-call
containers only, then repairs only JSON argument payloads. This mirrors the
provider-boundary shape used in ``pi``: normalize a tool-call block first,
parse/repair its arguments second.
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from src.lib.logging import get_logger

_LOG = get_logger(__name__)


@dataclass(frozen=True)
class ToolCallCandidate:
    """Normalized text fallback tool-call block."""

    id: str
    name: str
    arguments: dict[str, Any]
    source: str
    raw_arguments: str | None = None
    prefix_text: str = ""


class ToolCallParseError(Exception):
    """Raised when text fallback cannot produce exactly one valid tool call."""

    def __init__(
        self,
        message: str,
        attempted_strategies: list[str] | None = None,
        error_category: "ErrorCategory | None" = None,
    ):
        super().__init__(message)
        self.attempted_strategies = attempted_strategies or []
        self.error_category = error_category


# ---------------------------------------------------------------------------
# JSON repair and parsing
# ---------------------------------------------------------------------------

_VALID_SIMPLE_JSON_ESCAPES = {'"', "\\", "/", "b", "f", "n", "r", "t"}
_HEX_DIGITS = set("0123456789abcdefABCDEF")
_JSON_NUMBER_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _escape_control_char(ch: str) -> str:
    if ch == "\b":
        return "\\b"
    if ch == "\f":
        return "\\f"
    if ch == "\n":
        return "\\n"
    if ch == "\r":
        return "\\r"
    if ch == "\t":
        return "\\t"
    return f"\\u{ord(ch):04x}"


def repair_json_string_literals(raw: str) -> str:
    """Repair invalid bytes inside JSON string literals.

    This intentionally does not convert Python repr syntax or search prose for
    JSON. It only makes JSON string payloads acceptable to ``json.loads`` by
    escaping raw control characters and invalid backslashes.
    """

    out: list[str] = []
    in_string = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if ch == '"':
            out.append(ch)
            in_string = False
            i += 1
            continue

        if ch == "\\":
            next_ch = raw[i + 1] if i + 1 < len(raw) else ""
            if next_ch == "u":
                unicode_digits = raw[i + 2 : i + 6]
                if len(unicode_digits) == 4 and all(digit in _HEX_DIGITS for digit in unicode_digits):
                    out.append("\\u")
                    out.append(unicode_digits)
                    i += 6
                    continue
            if next_ch and next_ch in _VALID_SIMPLE_JSON_ESCAPES:
                out.append("\\")
                out.append(next_ch)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue

        if ord(ch) < 0x20:
            out.append(_escape_control_char(ch))
        else:
            out.append(ch)
        i += 1

    return "".join(out)


def parse_json_with_repair(raw: str) -> Any:
    """Parse JSON, retrying once with pi-style string-literal repair."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_error:
        repaired = repair_json_string_literals(raw)
        if repaired == raw:
            raise first_error
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            raise first_error


def _parse_arguments(value: Any) -> tuple[dict[str, Any], str | None]:
    """Parse a tool arguments payload into an object."""

    raw_arguments = value if isinstance(value, str) else None
    parsed = value
    if value is None:
        return {}, raw_arguments
    if isinstance(value, str):
        parsed = parse_json_with_repair(value)
        if isinstance(parsed, str):
            parsed = parse_json_with_repair(parsed)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed, raw_arguments


# ---------------------------------------------------------------------------
# Balanced structure extraction
# ---------------------------------------------------------------------------


def _extract_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    if start >= len(text) or text[start] != open_ch:
        return None

    depth = 0
    in_string = False
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1

    return None


def _iter_balanced_spans(text: str, open_ch: str, close_ch: str) -> Iterable[tuple[int, int, str]]:
    i = 0
    while i < len(text):
        if text[i] != open_ch:
            i += 1
            continue
        raw = _extract_balanced(text, i, open_ch, close_ch)
        if raw is None:
            i += 1
            continue
        yield i, i + len(raw), raw
        i += len(raw)


# ---------------------------------------------------------------------------
# Candidate normalization
# ---------------------------------------------------------------------------


def _normalize_direct_tool_call(
    data: dict[str, Any],
    *,
    source: str,
    prefix_text: str,
    tool_name_key: str,
    tool_arguments_key: str,
    call_id: str | None = None,
) -> ToolCallCandidate | None:
    tool_name = data.get(tool_name_key)
    if not isinstance(tool_name, str) or not tool_name:
        return None
    arguments, raw_arguments = _parse_arguments(data.get(tool_arguments_key))
    return ToolCallCandidate(
        id=call_id or str(uuid.uuid4()),
        name=tool_name,
        arguments=arguments,
        source=source,
        raw_arguments=raw_arguments,
        prefix_text=prefix_text.strip(),
    )


def _normalize_native_tool_call(
    data: Any,
    *,
    source: str,
    prefix_text: str,
    tool_name_key: str,
    tool_arguments_key: str,
) -> list[ToolCallCandidate]:
    if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
        return [
            candidate
            for item in data["tool_calls"]
            for candidate in _normalize_native_tool_call(
                item,
                source=source,
                prefix_text=prefix_text,
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            )
        ]

    if isinstance(data, list):
        return [
            candidate
            for item in data
            for candidate in _normalize_native_tool_call(
                item,
                source=source,
                prefix_text=prefix_text,
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            )
        ]

    if not isinstance(data, dict):
        return []

    function = data.get("function")
    if not isinstance(function, dict):
        return []

    candidate = _normalize_direct_tool_call(
        function,
        source=source,
        prefix_text=prefix_text,
        tool_name_key=tool_name_key,
        tool_arguments_key=tool_arguments_key,
        call_id=data.get("id") if isinstance(data.get("id"), str) else None,
    )
    return [candidate] if candidate else []


def _parse_native_dump(raw: str) -> Any:
    """Parse provider-dumped native tool call structures.

    ``ast.literal_eval`` is intentionally limited to native dump containers
    containing a ``function`` key. It is not used to accept arbitrary Python
    dict syntax for direct text fallback.
    """

    if "function" not in raw:
        return None
    try:
        return parse_json_with_repair(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError, RecursionError):
        return None


def _collect_json_candidates(
    text: str,
    *,
    tool_name_key: str,
    tool_arguments_key: str,
) -> list[ToolCallCandidate]:
    candidates: list[ToolCallCandidate] = []

    for start, _end, raw in _iter_balanced_spans(text, "[", "]"):
        data = _parse_native_dump(raw)
        if data is None:
            continue
        candidates.extend(
            _normalize_native_tool_call(
                data,
                source="native_dump",
                prefix_text=text[:start],
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            )
        )

    for start, _end, raw in _iter_balanced_spans(text, "{", "}"):
        try:
            data = parse_json_with_repair(raw)
        except json.JSONDecodeError:
            data = _parse_native_dump(raw)
        if data is None:
            continue
        if isinstance(data, dict):
            direct = _normalize_direct_tool_call(
                data,
                source="json",
                prefix_text=text[:start],
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            )
            if direct:
                candidates.append(direct)
            candidates.extend(
                _normalize_native_tool_call(
                    data,
                    source="native_dump",
                    prefix_text=text[:start],
                    tool_name_key=tool_name_key,
                    tool_arguments_key=tool_arguments_key,
                )
            )

    return candidates


# ---------------------------------------------------------------------------
# XML / invoke containers
# ---------------------------------------------------------------------------

_XML_WRAPPER_RE = re.compile(r"<((?:[\w.-]+:)?[\w.-]*tool[\w.-]*)>\s*(.*?)\s*</\1>", re.DOTALL)
_NAME_RE = re.compile(r"<(?:tool_)?name>\s*([\w.-]+)\s*</(?:tool_)?name>", re.DOTALL)
_ARGS_RE = re.compile(r"<arguments>\s*(.*?)\s*</arguments>", re.DOTALL)
_INVOKE_RE = re.compile(r'<invoke\s+name\s*=\s*"([\w.-]+)"\s*>(.*?)</invoke>', re.DOTALL)
_BRACKET_INVOKE_RE = re.compile(r'\[invoke\s+name\s*=\s*"([\w.-]+)"\s*>(.*?)\[/invoke\]', re.DOTALL)
_PARAM_RE = re.compile(
    r'<parameter\b[^>]*\bname\s*=\s*"([^"]+)"[^>]*>(.*?)</parameter>',
    re.DOTALL,
)


def _parse_xml_arguments(raw_args: str) -> dict[str, Any]:
    raw_args = raw_args.strip()
    if not raw_args:
        return {}

    args_match = _ARGS_RE.search(raw_args)
    if args_match:
        args, _raw = _parse_arguments(args_match.group(1).strip())
        return args

    params = _PARAM_RE.findall(raw_args)
    if params:
        result: dict[str, Any] = {}
        for key, raw_value in params:
            value = raw_value.strip()
            if not value:
                result[key.strip()] = ""
                continue
            try:
                result[key.strip()] = parse_json_with_repair(value)
            except json.JSONDecodeError:
                result[key.strip()] = value
        return result

    args, _raw = _parse_arguments(raw_args)
    return args


def _find_matching_close_tag(text: str, tag_name: str, start: int) -> int:
    return text.find(f"</{tag_name}>", start)


def _normalize_xml_name_arguments(
    text: str,
    *,
    source: str,
    prefix_text: str,
) -> ToolCallCandidate | None:
    name_match = _NAME_RE.search(text)
    if not name_match:
        return None
    return ToolCallCandidate(
        id=str(uuid.uuid4()),
        name=name_match.group(1),
        arguments=_parse_xml_arguments(text[name_match.end() :]),
        source=source,
        prefix_text=prefix_text.strip(),
    )


def _normalize_invoke(name: str, body: str, *, source: str, prefix_text: str) -> ToolCallCandidate:
    return ToolCallCandidate(
        id=str(uuid.uuid4()),
        name=name,
        arguments=_parse_xml_arguments(body),
        source=source,
        prefix_text=prefix_text.strip(),
    )


def _collect_xml_candidates(text: str) -> list[ToolCallCandidate]:
    candidates: list[ToolCallCandidate] = []

    for match in _XML_WRAPPER_RE.finditer(text):
        inner = match.group(2).strip()
        prefix_text = text[: match.start()]
        if inner.startswith(("[", "{")) and "function" in inner:
            data = _parse_native_dump(inner)
            if data is not None:
                candidates.extend(
                    _normalize_native_tool_call(
                        data,
                        source="xml_native_dump",
                        prefix_text=prefix_text,
                        tool_name_key="name",
                        tool_arguments_key="arguments",
                    )
                )
                continue
        candidate = _normalize_xml_name_arguments(inner, source="xml", prefix_text=prefix_text)
        if candidate:
            candidates.append(candidate)

    for pattern, source in ((_INVOKE_RE, "xml_invoke"), (_BRACKET_INVOKE_RE, "bracket_invoke")):
        for match in pattern.finditer(text):
            candidates.append(
                _normalize_invoke(
                    match.group(1),
                    match.group(2),
                    source=source,
                    prefix_text=text[: match.start()],
                )
            )

    return candidates


def _dedupe_candidates(candidates: list[ToolCallCandidate]) -> list[ToolCallCandidate]:
    deduped: list[ToolCallCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        signature = (candidate.name, json.dumps(candidate.arguments, sort_keys=True, default=str))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(candidate)
    return deduped


def _collect_candidates(
    text: str,
    *,
    tool_name_key: str,
    tool_arguments_key: str,
) -> tuple[list[str], list[str], list[ToolCallCandidate]]:
    attempted = ["xml_containers", "json_or_native_containers"]
    failures: list[str] = []
    candidates: list[ToolCallCandidate] = []

    for collector_name, collector in (
        ("xml_containers", lambda: _collect_xml_candidates(text)),
        (
            "json_or_native_containers",
            lambda: _collect_json_candidates(
                text,
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            ),
        ),
    ):
        try:
            new_candidates = collector()
        except Exception as exc:
            failures.append(f"{collector_name}: {type(exc).__name__}: {exc}")
            continue
        if not new_candidates:
            failures.append(f"{collector_name}: no explicit tool-call container")
        candidates.extend(new_candidates)

    return attempted, failures, _dedupe_candidates(candidates)


def parse_structured_tool_call(
    text: str,
    available_tool_names: Sequence[str] | None = None,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
    model_id: str | None = None,
) -> ToolCallCandidate:
    """Parse exactly one explicit structured tool call from model text output."""

    del model_id
    if not text or not text.strip():
        raise ToolCallParseError("Empty or whitespace-only model output.", attempted_strategies=[])

    attempted, failures, candidates = _collect_candidates(
        text,
        tool_name_key=tool_name_key,
        tool_arguments_key=tool_arguments_key,
    )

    if available_tool_names is not None:
        allowed = set(available_tool_names)
        for candidate in candidates:
            if candidate.name not in allowed:
                failures.append(f"tool '{candidate.name}' not in registered tools {list(available_tool_names)}")
        candidates = [candidate for candidate in candidates if candidate.name in allowed]

    if len(candidates) == 1:
        candidate = candidates[0]
        _LOG.debug("Tool call parsed: source=%s tool=%s", candidate.source, candidate.name)
        return candidate

    from src.lib.smolagents.error_recovery import classify_parse_error, extract_tool_info, ErrorCategory

    partial_tool_name = extract_tool_info(
        failures=failures,
        raw_text=text,
        available_tool_names=list(available_tool_names) if available_tool_names else None,
    )
    if len(candidates) > 1:
        failures.append(f"ambiguous multiple tool calls: {[c.name for c in candidates]}")
        error_category = ErrorCategory.ARGUMENT_ERROR
        diagnostic = f" Multiple tool calls are not supported in text fallback: {[c.name for c in candidates]}."
    else:
        error_category = classify_parse_error(
            failures=failures,
            partial_tool_name=partial_tool_name,
            available_tool_names=list(available_tool_names) if available_tool_names else None,
        )
        if error_category == ErrorCategory.UNKNOWN_TOOL and partial_tool_name:
            diagnostic = f" Tool '{partial_tool_name}' not found in registered tools."
        elif partial_tool_name:
            diagnostic = f" Could not parse tool call arguments. partial_tool={partial_tool_name}"
        else:
            diagnostic = " Could not parse a structured tool call."

    category_tag = f"[CATEGORY:{error_category.value}]" if error_category else ""
    _LOG.info("%s%s\nFull model output:\n%s", category_tag, diagnostic, text)
    raise ToolCallParseError(
        f"{category_tag}{diagnostic}\nYour output:\n{text}",
        attempted_strategies=attempted,
        error_category=error_category,
    )
