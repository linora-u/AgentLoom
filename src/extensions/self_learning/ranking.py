"""Pure ranking and conflict heuristics for memory snapshot selection.

No I/O here: every function takes plain values and returns deterministic
results so ranking behavior is trivially testable and reproducible.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_WORD_RE = re.compile(r"[\w./:-]{2,}", flags=re.UNICODE)
_DECAY_HALF_LIFE_DAYS = 30.0
_DECAY_FLOOR = 0.1

# Tiny stopword list: only words so common they carry no task signal.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "are", "was",
    "were", "has", "have", "had", "not", "but", "can", "will", "use", "using",
    "you", "your", "all", "any", "one", "two", "then", "than", "when", "how",
    "的", "了", "和", "与", "在", "是", "对", "个", "把", "被", "并", "或",
}


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x3400 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF


def _cjk_bigrams(text: str) -> set[str]:
    """Character bigrams over every contiguous CJK run (single-char runs kept whole)."""
    tokens: set[str] = set()
    run = ""
    for ch in str(text or "") + "\x00":  # sentinel flushes the trailing run
        if _is_cjk(ch):
            run += ch
            continue
        if len(run) == 1 and run not in _STOPWORDS:
            tokens.add(run)
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in _STOPWORDS:
                tokens.add(bigram)
        run = ""
    return tokens


def tokenize(text: str) -> set[str]:
    """Word tokens (len>=2, lowercased) plus CJK character bigrams."""
    tokens = {t.lower() for t in _WORD_RE.findall(str(text or ""))}
    tokens -= _STOPWORDS
    return tokens | _cjk_bigrams(text)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def temporal_decay(
    updated_at: str,
    *,
    half_life_days: float = _DECAY_HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> float:
    """Exponential forgetting curve, floored so old trusted facts never vanish."""
    try:
        updated = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return 1.0
    reference = now or datetime.now(updated.tzinfo)
    if updated.tzinfo is None and reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    age_days = max(0.0, (reference - updated).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
    return max(_DECAY_FLOOR, min(1.0, decay))


def score_item(item: dict[str, Any], task_tokens: set[str], *, now: datetime | None = None) -> float:
    """relevance x trust x recency. Without task tokens relevance is a constant,
    so ordering degenerates to trust x decay (approximately newest-first)."""
    relevance = 0.5 + 0.5 * jaccard(task_tokens, tokenize(str(item.get("content") or "")))
    trust = float(item.get("trust_score") if item.get("trust_score") is not None else 0.5)
    return relevance * trust * temporal_decay(str(item.get("updated_at") or ""), now=now)


def rank_items(
    items: list[dict[str, Any]],
    task_text: str = "",
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Deterministic ordering: score desc, then updated_at desc, then id desc."""
    task_tokens = tokenize(task_text) if task_text else set()
    return sorted(
        items,
        key=lambda it: (
            score_item(it, task_tokens, now=now),
            str(it.get("updated_at") or ""),
            int(it.get("id") or 0),
        ),
        reverse=True,
    )


_CONTENT_WORD_RE = re.compile(r"[\w./:-]{3,}|\d+", flags=re.UNICODE)
_CONFLICT_MIN_OVERLAP = 0.25
_CONFLICT_LIMIT = 3

def _content_tokens(text: str) -> set[str]:
    """Content-bearing tokens: longer words, numbers, paths, plus CJK bigrams.

    Trailing punctuation is stripped ("minute." == "minute") so sentence-final
    tokens compare equal across rewordings; interior dots survive, keeping
    paths and versions ("config.yaml", "v1.2") intact.
    """
    tokens = {t.lower().rstrip("./:-") for t in _CONTENT_WORD_RE.findall(str(text or ""))}
    tokens -= _STOPWORDS
    tokens.discard("")
    return tokens | _cjk_bigrams(text)


def content_overlap(a: str, b: str) -> float:
    """Jaccard overlap of content-bearing tokens between two texts."""
    return jaccard(_content_tokens(a), _content_tokens(b))


def find_conflicts(content: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag active items whose content overlaps enough to plausibly contradict.

    High token overlap between two DIFFERENT texts about the same subject is
    the contradiction signal. There is no upper bound: exact duplicates never
    coexist (the content-hash dedup index blocks them at insert), so a >=0.9
    pair that reached this scan is a long text differing in a few values —
    precisely the contradiction that must not slip through.
    Returns at most 3, strongest first.
    """
    content_tokens = _content_tokens(content)
    if not content_tokens:
        return []
    conflicts: list[dict[str, Any]] = []
    for candidate in candidates:
        overlap = jaccard(content_tokens, _content_tokens(str(candidate.get("content") or "")))
        if overlap >= _CONFLICT_MIN_OVERLAP:
            conflicts.append(
                {
                    "id": int(candidate.get("id") or 0),
                    "score": round(overlap, 3),
                    "content_preview": str(candidate.get("content") or "")[:120],
                }
            )
    conflicts.sort(key=lambda c: (-c["score"], c["id"]))
    return conflicts[:_CONFLICT_LIMIT]
