"""
Markdown file writer tools for AI agents.

Problem solved:
    When a CodeAgent generates Python code to write long Markdown content,
    the LLM often produces syntactically invalid Python due to complex string
    escaping issues (unmatched quotes, missing '+' in concatenation, special
    characters like em-dash, pipe, arrow, etc.). This causes ast.parse() to
    fail with SyntaxError before the code can even execute.

Solution:
    Provide structured APIs that accept simple data structures (lists of dicts)
    instead of requiring the LLM to embed raw Markdown in Python string literals.
    The tool handles all formatting internally.

    Three complementary approaches:
    1. write_markdown_file       - structured sections (heading + body dicts)
    2. write_markdown_file_raw   - base64 encoded content (zero escaping issues)
    3. append_markdown_sections  - incremental building across multiple LLM steps

References:
    - smolagents CodeAgent: LLM generates Python -> ast.parse() -> evaluate_ast()
    - The SyntaxError happens at ast.parse() level, not at runtime string level
    - Real failure case from logs:
        "# 补充信息需求报告\\n\\n"     ^
        Error: invalid syntax. Perhaps you forgot a comma?
"""

import base64
import json
from src.lib.logging import get_logger

logger = get_logger(__name__)
from pathlib import Path
from typing import List


def write_markdown_file(
    file_path: str,
    sections: List[dict],
    title: str = "",
    metadata: dict = {},
    overwrite: bool = True,
    create_directories: bool = True,
    encoding: str = "utf-8",
) -> str:
    """
    Write a structured Markdown file from sections data.

    This tool creates a Markdown file from structured section data, avoiding
    the need for LLM to construct complex Markdown strings in Python code.
    Each section is a dict with 'heading', 'level', and 'body' keys.

    This is the PREFERRED way for AI agents to write Markdown reports, as it
    eliminates Python string escaping issues that cause SyntaxError in CodeAgent.

    Args:
        file_path: The absolute or relative path where the Markdown file should be created.
        sections: A list of section dicts. Each dict can have:
            - 'heading' (str, optional): The section heading text.
            - 'level' (int, optional): Heading level 1-6, default 2.
            - 'body' (str, optional): The section body content (can include Markdown).
        title: Optional document title (rendered as H1 at the top).
        metadata: Optional dict of key-value pairs rendered as a blockquote header.
            Example: {"author": "AI Agent", "date": "2026-03-08"}
        overwrite: Whether to overwrite existing file. Default True.
        create_directories: Whether to create parent directories. Default True.
        encoding: File encoding. Default utf-8.

    Returns:
        str: Success message with file path and size information.

    Raises:
        ValueError: If file_path is empty, sections is empty or contains non-dict items.
        FileExistsError: If file exists and overwrite is False.
        OSError: If there is an error creating directories or writing the file.

    Examples:
        >>> write_markdown_file("report.md", [
        ...     {"heading": "Introduction", "level": 2, "body": "This is the intro."},
        ...     {"heading": "Results", "level": 2, "body": "| Col1 | Col2 |\\n|---|---|\\n| A | B |"},
        ... ], title="My Report")
    """
    # --- Validate inputs ---
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")
    file_path = file_path.strip()

    # --- Coerce stringified JSON parameters from LLM ---
    if isinstance(sections, str):
        try:
            sections = json.loads(sections)
        except (json.JSONDecodeError, ValueError):
            raise ValueError(
                "sections must be a list of dicts. Got a string that "
                "could not be parsed as JSON."
            )
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            raise ValueError(
                "metadata must be a dict. Got a string that "
                "could not be parsed as JSON."
            )

    if not sections:
        raise ValueError("sections list is required and cannot be empty")

    path = Path(file_path)

    if path.is_dir():
        raise ValueError(f"Path '{file_path}' is a directory, not a file")

    if path.exists() and not overwrite:
        raise FileExistsError(
            f"File '{file_path}' already exists. Use overwrite=True to replace it."
        )

    # --- Build the Markdown content from structured data ---
    parts: List[str] = []

    # Title
    if title:
        parts.append(f"# {title}")
        parts.append("")

    # Metadata block
    if metadata:
        for key, value in metadata.items():
            parts.append(f"> **{key}**: {value}")
        parts.append("")
        parts.append("---")
        parts.append("")

    # Sections
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(
                f"Section at index {i} must be a dict, got {type(section).__name__}"
            )

        heading = section.get("heading", "")
        level = section.get("level", 2)
        body = section.get("body", "")

        # Clamp level to valid Markdown range
        if not isinstance(level, int) or level < 1 or level > 6:
            level = 2

        # Add heading
        if heading:
            prefix = "#" * level
            parts.append(f"{prefix} {heading}")
            parts.append("")

        # Add body
        if body:
            parts.append(body)
            parts.append("")

    content = "\n".join(parts)

    # Write file
    return _write_to_file(path, content, create_directories, encoding)


def write_markdown_file_raw(
    file_path: str,
    content_b64: str = "",
    content_plain: str = "",
    overwrite: bool = True,
    create_directories: bool = True,
    encoding: str = "utf-8",
) -> str:
    """
    Write a Markdown file from base64-encoded or plain text content.

    Use this tool when you have pre-formatted Markdown content. The base64
    mode avoids Python string escaping issues entirely: encode your Markdown
    content as base64 first, then pass it here.

    Either content_b64 or content_plain must be provided (not both empty).
    If both are provided, content_b64 takes priority.

    Args:
        file_path: The absolute or relative path where the Markdown file should be created.
        content_b64: Base64-encoded UTF-8 Markdown content. Takes priority over content_plain.
        content_plain: Plain text Markdown content (fallback if content_b64 is empty).
        overwrite: Whether to overwrite existing file. Default True.
        create_directories: Whether to create parent directories. Default True.
        encoding: File encoding. Default utf-8.

    Returns:
        str: Success message with file path and size information.

    Raises:
        ValueError: If both content_b64 and content_plain are empty, or content_b64 is invalid.
        FileExistsError: If file exists and overwrite is False.
        OSError: If there is an error creating directories or writing the file.

    Examples:
        >>> import base64
        >>> md = "# Title\\n\\nHello world"
        >>> b64 = base64.b64encode(md.encode()).decode()
        >>> write_markdown_file_raw("doc.md", content_b64=b64)
    """
    # --- Validate inputs ---
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")

    if not content_b64 and not content_plain:
        raise ValueError(
            "Either content_b64 or content_plain must be provided"
        )

    path = Path(file_path)

    if path.is_dir():
        raise ValueError(f"Path '{file_path}' is a directory, not a file")

    if path.exists() and not overwrite:
        raise FileExistsError(
            f"File '{file_path}' already exists. Use overwrite=True to replace it."
        )

    # --- Decode content ---
    if content_b64:
        try:
            content = base64.b64decode(content_b64).decode(encoding)
        except Exception as e:
            raise ValueError(
                f"Failed to decode base64 content: {e}. "
                "Ensure content_b64 is valid base64-encoded UTF-8 text."
            )
    else:
        content = content_plain

    return _write_to_file(path, content, create_directories, encoding)


def append_markdown_sections(
    file_path: str,
    sections: List[dict],
    encoding: str = "utf-8",
) -> str:
    """
    Append structured sections to an existing Markdown file.

    This tool appends new sections to the end of an existing Markdown file.
    Useful for building reports incrementally across multiple LLM steps,
    which also keeps each step's code shorter and less error-prone.

    Args:
        file_path: Path to the existing Markdown file.
        sections: A list of section dicts (same format as write_markdown_file).
            Each dict can have:
            - 'heading' (str, optional): The section heading text.
            - 'level' (int, optional): Heading level 1-6, default 2.
            - 'body' (str, optional): The section body content.
        encoding: File encoding. Default utf-8.

    Returns:
        str: Success message with appended size information.

    Raises:
        ValueError: If file_path is empty or sections is empty/invalid.
        FileNotFoundError: If the target file does not exist.
        OSError: If there is a write error.

    Examples:
        >>> append_markdown_sections("report.md", [
        ...     {"heading": "New Section", "level": 2, "body": "Additional content here."},
        ... ])
    """
    # --- Validate inputs ---
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")

    if not sections:
        raise ValueError("sections list is required and cannot be empty")

    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File '{file_path}' does not exist")

    if path.is_dir():
        raise ValueError(f"Path '{file_path}' is a directory, not a file")

    # --- Build new content ---
    parts: List[str] = [""]  # Start with blank line as separator

    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(
                f"Section at index {i} must be a dict, got {type(section).__name__}"
            )

        heading = section.get("heading", "")
        level = section.get("level", 2)
        body = section.get("body", "")

        if not isinstance(level, int) or level < 1 or level > 6:
            level = 2

        if heading:
            prefix = "#" * level
            parts.append(f"{prefix} {heading}")
            parts.append("")

        if body:
            parts.append(body)
            parts.append("")

    new_content = "\n".join(parts)

    try:
        with open(path, "a", encoding=encoding) as f:
            f.write(new_content)

        logger.info(f"Appended to Markdown file: {path} ({len(new_content)} chars)")
        return (
            f"Successfully appended {len(new_content)} chars to '{file_path}'. "
            f"Sections added: {len(sections)}."
        )

    except Exception as e:
        error_msg = f"Failed to append to file '{file_path}': {e}"
        logger.error(error_msg)
        raise OSError(error_msg) from e


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _write_to_file(
    path: Path,
    content: str,
    create_directories: bool,
    encoding: str,
) -> str:
    """Internal helper to write content to a file with directory creation."""
    try:
        if create_directories and path.parent != path:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created directories: {path.parent}")

        with open(path, "w", encoding=encoding) as f:
            f.write(content)

        file_size = path.stat().st_size
        logger.info(f"Created Markdown file: {path} ({file_size} bytes)")

        return (
            f"Successfully wrote Markdown file '{path}' "
            f"({file_size} bytes, {len(content)} chars, encoding: {encoding})"
        )

    except PermissionError as e:
        error_msg = f"Permission denied when creating file '{path}': {e}"
        logger.error(error_msg)
        raise PermissionError(error_msg) from e

    except OSError as e:
        error_msg = f"Failed to create file '{path}': {e}"
        logger.error(error_msg)
        raise OSError(error_msg) from e
