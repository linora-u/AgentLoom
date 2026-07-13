"""Secret redaction and injection scanning shared by self-learning storage and tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from typing import Any

_REDACTED = "[REDACTED]"
BLOCKED_TEXT = "[BLOCKED]"
_SCAN_CHUNK_CHARS = 65_536
_SCAN_OVERLAP_CHARS = 512
_MEMORY_CAMPAIGN_SAFE_ARTIFACTS_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_SAFE_ARTIFACTS"

# Invisible / bidirectional-control codepoints used to smuggle injection text
# past regex scanning (e.g. "ig<ZWSP>nore previous instructions"). Checked on
# the RAW text BEFORE NFKC normalization, since normalization strips some.
_INVISIBLE_CHARS = frozenset(
    {
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\u2060",  # word joiner
        "\u2062",  # invisible times
        "\u2063",  # invisible separator
        "\u2064",  # invisible plus
        "\ufeff",  # zero-width no-break space (BOM)
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",  # LTR/RTL embedding/override
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",  # LTR/RTL/first-strong/pop isolates
    }
)
# ZWNJ and ZWJ are meaningful shaping controls in Persian/Indic text and emoji
# sequences.  They remain part of the stripped regex view so they cannot split
# an injection phrase, but their mere presence is not itself an injection.
_CONTEXTUAL_JOINER_CHARS = frozenset({"\u200c", "\u200d"})
_UNCONDITIONAL_INVISIBLE_CHARS = _INVISIBLE_CHARS - _CONTEXTUAL_JOINER_CHARS

_PREFIX_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Header-shaped values may be very short in fixtures. Length is not a
    # security property, so redact the credential regardless of size.
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*basic\s+)[^\s,;]+"),
)

_SENSITIVE_SEGMENTS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "pwd",
        "cookie",
        "authorization",
        "credential",
        "credentials",
    }
)
_SENSITIVE_KEY_PREFIXES = frozenset(
    {
        "access",
        "api",
        "app",
        "auth",
        "azure",
        "client",
        "encryption",
        "gcp",
        "github",
        "gitlab",
        "openai",
        "private",
        "secret",
        "service",
        "signing",
        "slack",
        "webhook",
        "aws",
    }
)
_SAFE_KEY_NAMES = frozenset({"sort_key", "token_count"})
_BARE_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:ark|volc|bearer)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

# High-precision prompt-injection shapes. Memory content matching any of these
# is withheld from prompt snapshots (the stored row is kept for inspection).
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override-instructions",
        re.compile(
            r"(?i)\b(ignore|disregard|forget)\s+"
            r"(?:(?:all|any)\s+)?(?:the\s+)?"
            r"(previous|prior|earlier|above)\s+"
            r"(instructions?|prompts?|rules?|context)"
        ),
    ),
    (
        "role-hijack",
        re.compile(
            r"(?i)\bnew\s+system\s+prompt\b|\byou\s+are\s+now\b.{0,60}\b(unrestricted|jailbroken|developer\s+mode)\b"
        ),
    ),
    # [^>]* allows attributes: the snapshot's own open tags carry them
    # (<app_memory application_id="...">), and a forged tag would too.
    (
        "fence-escape",
        re.compile(
            r"(?i)</?\s*(agentloom_memory_snapshot|project_memory|app_memory|session_memory|system|assistant)\b[^>]*>"
        ),
    ),
    ("pipe-to-shell", re.compile(r"(?i)\b(curl|wget)\b[^|\n]{0,200}\|\s*(ba|z)?sh\b")),
    ("destructive-shell", re.compile(r"(?i)\brm\s+-rf\s+[/~]")),
)


def scan_injection_patterns(text: str) -> list[str]:
    """Return the ids of injection patterns found in ``text`` (empty when clean).

    Invisible codepoints are detected on the raw text; the regex pass then runs
    on NFKC-folded text (defeats full-width homographs like "ｉｇｎｏｒｅ") with
    any invisibles stripped, so an interleaved zero-width bypass also trips the
    underlying pattern id.
    """
    findings = []
    value = str(text or "")
    if BLOCKED_TEXT in value:
        findings.append("blocked-fragment")
    raw_characters = set(value)
    pattern_hits: set[str] = set()
    for start in range(0, max(1, len(value)), _SCAN_CHUNK_CHARS):
        lower = max(0, start - _SCAN_OVERLAP_CHARS)
        upper = min(len(value), start + _SCAN_CHUNK_CHARS)
        chunk = "".join(character for character in value[lower:upper] if character not in _INVISIBLE_CHARS)
        normalized = unicodedata.normalize("NFKC", chunk)
        for pattern_id, pattern in _INJECTION_PATTERNS:
            if pattern.search(normalized):
                pattern_hits.add(pattern_id)
    if raw_characters & _UNCONDITIONAL_INVISIBLE_CHARS or (
        raw_characters & _CONTEXTUAL_JOINER_CHARS and pattern_hits
    ):
        findings.append("invisible-unicode")
    for pattern_id, _pattern in _INJECTION_PATTERNS:
        if pattern_id in pattern_hits:
            findings.append(pattern_id)
    return findings


def _normalized_key(value: Any) -> str:
    """Canonicalize a structured key without conflating unrelated words.

    NFKC defeats full-width spelling, the camel-case split makes
    ``clientSecret`` equivalent to ``client_secret``, and all remaining
    separators collapse to one underscore.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"[^0-9A-Za-z]+", "_", text.casefold()).strip("_")
    return text


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    if not normalized or normalized in _SAFE_KEY_NAMES:
        return False
    if normalized in {"apikey", "appkey", "authkey", "privatekey", "accesskey"}:
        return True
    parts = tuple(part for part in normalized.split("_") if part)
    if any(part in _SENSITIVE_SEGMENTS for part in parts):
        return True
    if "token" in parts:
        return True
    if len(parts) < 2 or parts[-1] != "key":
        return False
    return parts[-2] in _SENSITIVE_KEY_PREFIXES or "".join(parts[:-1]) in _SENSITIVE_KEY_PREFIXES


def _syntax_char(value: str) -> str:
    """Fold one syntax codepoint without changing offsets in the source text."""
    if value.isascii():
        return value
    normalized = unicodedata.normalize("NFKC", value)
    return normalized if len(normalized) == 1 else value


def _is_assignment_separator(value: str) -> bool:
    if value in {":", "="}:
        return True
    return not value.isascii() and _syntax_char(value) in {":", "="}


def _is_key_boundary(value: str) -> bool:
    return value in "\r\n" or _syntax_char(value) in "{}[],;:=\"'"


def _key_before_separator(text: str, separator_index: int) -> tuple[int, str] | None:
    """Parse one key immediately to the left of ``:`` or ``=``.

    Candidate discovery starts from separators instead of retrying a greedy
    key regex at every character.  That distinction keeps safe long fragments
    linear while still exposing assignments nested inside an outer string.
    """
    end = separator_index
    while end > 0 and text[end - 1].isspace():
        end -= 1
    if end == 0:
        return None

    # Common code/config renderings put the sensitive key in a quoted
    # subscript rather than directly beside the assignment separator:
    # ``headers["Authorization"] = value`` and
    # ``os.environ [ 'API_KEY' ] = value``. Parse only the final, quoted
    # subscript component. This is still separator-driven and each candidate
    # consumes one bounded key span, so safe long fragments remain linear.
    if _syntax_char(text[end - 1]) == "]":
        cursor = end - 1
        while cursor > 0 and text[cursor - 1] in " \t":
            cursor -= 1
        if cursor > 0 and text[cursor - 1] in {'"', "'"}:
            quote = text[cursor - 1]
            key_end = cursor - 1
            start = key_end
            while start > 0:
                if text[start - 1] == quote and (start < 2 or text[start - 2] != "\\"):
                    break
                start -= 1
            opening_quote = start - 1
            bracket = opening_quote
            while bracket > 0 and text[bracket - 1] in " \t":
                bracket -= 1
            if start < key_end and opening_quote >= 0 and bracket > 0 and _syntax_char(text[bracket - 1]) == "[":
                return opening_quote, text[start:key_end]

    key_end = end
    if text[end - 1] in {'"', "'"}:
        quote = text[end - 1]
        key_end -= 1
        start = key_end
        while start > 0:
            if text[start - 1] == quote and (start < 2 or text[start - 2] != "\\"):
                break
            start -= 1
        if start == key_end or start == 0:
            return None
        return start - 1, text[start:key_end]

    start = key_end
    while start > 0 and not _is_key_boundary(text[start - 1]):
        start -= 1
    while start < key_end and text[start].isspace():
        start += 1
    # Preserve YAML sequence/explicit-key markers; they are structure, not
    # part of a key such as ``- api key`` or ``? client secret``.
    if start + 1 < key_end and text[start] in {"-", "?"} and text[start + 1].isspace():
        start += 1
        while start < key_end and text[start].isspace():
            start += 1
    while key_end > start and text[key_end - 1].isspace():
        key_end -= 1
    if start == key_end:
        return None
    return start, text[start:key_end]


def _line_start(text: str, index: int) -> int:
    return max(text.rfind("\n", 0, index), text.rfind("\r", 0, index)) + 1


def _next_line_start(text: str, line_end: int) -> int:
    cursor = line_end
    if cursor < len(text) and text[cursor] == "\r":
        cursor += 1
    if cursor < len(text) and text[cursor] == "\n":
        cursor += 1
    return cursor


def _scan_yaml_block(
    text: str,
    first_line_end: int,
    *,
    key_column: int,
) -> int:
    """Consume the indented YAML node following a mapping key."""
    cursor = _next_line_start(text, first_line_end)
    value_end = first_line_end
    block_indent: int | None = None
    indentless_sequence = False
    while cursor < len(text):
        line_end = cursor
        while line_end < len(text) and text[line_end] not in "\r\n":
            line_end += 1
        line = text[cursor:line_end]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            value_end = line_end
            cursor = _next_line_start(text, line_end)
            continue
        indent = len(line) - len(stripped)
        if block_indent is None:
            if indent > key_column:
                block_indent = indent
            elif indent == key_column and stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace()):
                block_indent = indent
                indentless_sequence = True
            else:
                break
        elif indentless_sequence:
            if indent == block_indent and not (
                stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace())
            ):
                break
        elif indent <= key_column:
            break
        value_end = line_end
        cursor = _next_line_start(text, line_end)
    return value_end


def _scan_flow_collection(text: str, start: int) -> int:
    """Return the end of one balanced YAML/JSON flow collection."""
    opening = _syntax_char(text[start])
    expected = {"[": "]", "{": "}"}[opening]
    stack = [expected]
    quote = ""
    index = start + 1
    while index < len(text):
        current = text[index]
        if quote:
            if current == "\\" and quote == '"':
                index = min(len(text), index + 2)
                continue
            if current == quote:
                if quote == "'" and index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if current in {'"', "'"}:
            quote = current
            index += 1
            continue
        if current == "#" and (index == start + 1 or text[index - 1].isspace()):
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        syntax = _syntax_char(current)
        if syntax in {"[", "{"}:
            stack.append({"[": "]", "{": "}"}[syntax])
        elif syntax in {"]", "}"}:
            if syntax != stack[-1]:
                return len(text)
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    # Truncated structured values fail closed through EOF.
    return len(text)


def _value_bounds(
    text: str,
    separator_index: int,
    *,
    key_start: int,
) -> tuple[int, int] | None:
    """Return the value span following a key/value separator."""
    start = separator_index + 1
    while start < len(text) and text[start] in " \t":
        start += 1
    if start >= len(text):
        return None

    key_column = key_start - _line_start(text, key_start)
    if text[start] in "\r\n":
        value_end = _scan_yaml_block(
            text,
            start,
            key_column=key_column,
        )
        return (start, value_end) if value_end > start else None

    quote = text[start] if text[start] in {'"', "'"} else ""
    if quote:
        index = start + 1
        while index < len(text):
            current = text[index]
            if current == "\\":
                index = min(len(text), index + 2)
                continue
            if current == quote:
                return start, index + 1
            index += 1
        return start, len(text)

    # YAML block scalars own their following indented lines. Redacting only
    # the ``|``/``>`` indicator would leave the actual credential untouched.
    header_end = start
    while header_end < len(text) and text[header_end] not in "\r\n":
        header_end += 1
    header = text[start:header_end]
    if re.fullmatch(
        r"[|>](?:[1-9][+-]?|[+-][1-9]?)?(?:[ \t]+#.*)?[ \t]*",
        header,
    ):
        value_end = _scan_yaml_block(
            text,
            header_end,
            key_column=key_column,
        )
        return start, value_end

    if _syntax_char(text[start]) in {"[", "{"}:
        return start, _scan_flow_collection(text, start)

    # An unquoted assignment owns the complete scalar through the line's
    # structural delimiter. Whitespace is part of the value, not a safe
    # boundary: ``password=two words`` must not preserve ``words``.
    index = start
    while index < len(text):
        current = text[index]
        if current in "\r\n,;}'\"\\":
            break
        index += 1
    if index < len(text) and text[index] in "\r\n":
        index = _scan_yaml_block(
            text,
            index,
            key_column=key_column,
        )
    return (start, index) if index > start else None


def _redact_key_values(text: str) -> str:
    """Redact overlapping key/value shapes, including secrets nested in text.

    A separator-driven scanner can inspect ``api_key`` inside a safe outer JSON
    string without the quadratic backtracking caused by a zero-width greedy
    regex. Once a sensitive value is found, its whole span is skipped because
    it will be replaced as one unit; candidate/value scans therefore remain
    disjoint even for adversarial repeated assignments.
    """
    candidates: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        current = text[index]
        if current not in {":", "="} and (current.isascii() or not _is_assignment_separator(current)):
            index += 1
            continue
        key = _key_before_separator(text, index)
        if key is None or not _is_sensitive_key(key[1]):
            index += 1
            continue
        value = _value_bounds(text, index, key_start=key[0])
        if value is None:
            index += 1
            continue
        value_start, value_end = value
        raw_value = text[value_start:value_end]
        quote = raw_value[0] if raw_value[:1] in {'"', "'"} else ""
        redacted_value = f"{quote}{_REDACTED}{quote}" if quote else _REDACTED
        candidates.append(
            (
                key[0],
                value_end,
                f"{text[key[0] : value_start]}{redacted_value}",
            )
        )
        index = max(index + 1, value_end)

    for start, end, replacement in reversed(candidates):
        text = text[:start] + replacement + text[end:]
    return text


def _redact_secret_shapes(text: str) -> str:
    """Apply secret rules without changing unrelated serialization bytes."""
    for pattern in _PREFIX_SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", text)
    text = _redact_key_values(text)
    for pattern in _BARE_SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def _quote_layer_candidates(text: str, *, max_layers: int = 8) -> list[str]:
    """Return bounded quote-unescaped views without mutating safe input."""
    candidates = [text]
    candidate = text
    slash = chr(92)
    for _ in range(max_layers):
        normalized = candidate.replace(slash + '"', '"').replace(slash + "'", "'")
        if normalized == candidate:
            break
        candidates.append(normalized)
        candidate = normalized
    return candidates


def _redact_structured_json(candidate: str) -> str | None:
    """Redact a JSON object/array as structure, or return None if safe."""
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    sanitized = redact_value(parsed)
    if sanitized == parsed:
        return None
    return json.dumps(sanitized, ensure_ascii=False, default=str)


def redact_text(value: Any, *, max_chars: int | None = None) -> str:
    """Return a string with common credential shapes replaced."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)

    # Safe escaped JSON keeps its exact bytes: normalized views are returned
    # only after a credential is found. Structural redaction owns the complete
    # sensitive value (including escaped quotes and non-string values). For
    # malformed/truncated payloads, inspect every bounded escape layer and use
    # the most conservative changed representation.
    candidates = _quote_layer_candidates(text)
    redacted = text
    structured_redactions = [
        result for candidate in candidates if (result := _redact_structured_json(candidate)) is not None
    ]
    if structured_redactions:
        redacted = structured_redactions[0]
    else:
        changed = [result for candidate in candidates if (result := _redact_secret_shapes(candidate)) != candidate]
        if changed:
            redacted = min(changed, key=len)

    if max_chars is not None and max_chars >= 0 and len(redacted) > max_chars:
        return redacted[:max_chars] + f"\n...[truncated {len(redacted) - max_chars} chars]"
    return redacted


def redact_value(value: Any) -> Any:
    """Recursively redact a JSON-like value.

    A sensitive key redacts its *entire* value, independent of value type,
    length, or formatting. Non-sensitive containers retain their type so this
    function is safe for callers that have not serialized their payload yet.
    Scalar strings still receive free-text credential scanning.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = (
                key
                if key is None or type(key) in {bool, int, float}
                else redact_text(key)
            )
            redacted[safe_key] = _REDACTED if _is_sensitive_key(key) else redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, set):
        return {redact_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    if value is None or type(value) in {bool, int, float}:
        return value
    # ``json.dumps(default=str)`` is a serialization boundary, not a safety
    # boundary. Convert arbitrary scalar objects while their string form can
    # still be redacted instead of letting a later serializer persist it raw.
    return redact_text(value)


def sanitize_text_fragment(value: Any, *, max_chars: int | None = None) -> str:
    """Redact one untrusted text fragment, replacing injections as a unit."""
    # Scan the complete redacted value before applying a storage/model size
    # cap. Otherwise an attacker can place the instruction one byte beyond the
    # truncation point and have the apparently safe prefix persisted/indexed.
    redacted = redact_text(value)
    if scan_injection_patterns(redacted):
        return BLOCKED_TEXT
    if max_chars is not None and max_chars >= 0 and len(redacted) > max_chars:
        return redacted[:max_chars] + f"\n...[truncated {len(redacted) - max_chars} chars]"
    return redacted


def require_safe_identity(
    value: Any,
    *,
    field: str = "identity",
    allow_empty: bool = False,
) -> str:
    """Return a storage identity only when sanitization would be lossless.

    Replacing a secret inside a primary/dedupe/reference key can collide two
    objects or silently detach their references. Runtime write seams therefore
    reject unsafe identities; the v4 migration uses deterministic hashed rekeys
    because it must preserve already-persisted relationships.
    """
    raw = str(value or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise ValueError(f"{field} is required")
    if sanitize_text_fragment(raw) != raw:
        raise ValueError(f"{field} contains sensitive or blocked text")
    return raw


def safe_storage_identity(
    value: Any,
    *,
    namespace: str,
    allow_empty: bool = False,
) -> str:
    """Return a deterministic non-secret identity for user/config namespaces."""
    raw = str(value or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise ValueError(f"{namespace} identity is required")
    if sanitize_text_fragment(raw) == raw:
        return raw
    digest = hashlib.sha256(
        raw.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return f"redacted-{namespace}-{digest}"


def sanitize_value_fragments(value: Any) -> Any:
    """Recursively redact values and block every injection-bearing string leaf."""
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = (
                key
                if key is None or type(key) in {bool, int, float}
                else sanitize_text_fragment(key)
            )
            sanitized[safe_key] = (
                _REDACTED
                if _is_sensitive_key(key)
                else (BLOCKED_TEXT if safe_key == BLOCKED_TEXT else sanitize_value_fragments(item))
            )
        return sanitized
    if isinstance(value, list):
        return [sanitize_value_fragments(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value_fragments(item) for item in value)
    if isinstance(value, set):
        return {sanitize_value_fragments(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(sanitize_value_fragments(item) for item in value)
    if isinstance(value, str):
        return sanitize_text_fragment(value)
    if value is None or type(value) in {bool, int, float}:
        return value
    return sanitize_text_fragment(value)


def sanitize_campaign_artifact_value(value: Any) -> Any:
    """Sanitize a validation payload before its first artifact serialization.

    Shell hooks have two independent execution adapters (declarative config
    hooks and skill-owned hooks).  Both must cross this same boundary before
    writing a temp file, environment variable, stdin stream, or passive
    visualization artifact.  Outside the isolated validation campaign the
    adapters retain their existing payload contract.
    """
    enabled = os.environ.get(_MEMORY_CAMPAIGN_SAFE_ARTIFACTS_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}
    return sanitize_value_fragments(value) if enabled else value


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Return a recursively sanitized mapping without a serialize/parse gap."""
    redacted = sanitize_value_fragments(value)
    return redacted if isinstance(redacted, dict) else {"value": redacted}
