"""Constrained reference reader for the news-catalyst Skill."""

from pathlib import Path
import re


_REFERENCE_ROOT = Path(__file__).resolve().parent / "skills" / "news-catalyst" / "references"
_NAME = re.compile(r"[a-z0-9-]+\.md\Z")


def read_news_reference(filename: str) -> str:
    """Read one news-catalyst reference by filename, never an arbitrary path."""
    if not isinstance(filename, str) or not _NAME.fullmatch(filename):
        raise ValueError("reference must be a news-catalyst Markdown filename")
    path = _REFERENCE_ROOT / filename
    if not path.is_file() or path.is_symlink() or path.resolve().parent != _REFERENCE_ROOT.resolve():
        raise ValueError(f"unknown news-catalyst reference: {filename}")
    return path.read_text(encoding="utf-8")
