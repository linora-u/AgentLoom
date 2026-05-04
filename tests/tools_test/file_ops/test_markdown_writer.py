"""Tests for markdown_writer tool.

Verifies that the structured Markdown writing tools work correctly,
including:
- write_markdown_file: structured sections -> .md file
- write_markdown_file_raw: base64/plain content -> .md file
- append_markdown_sections: append sections to existing .md file
- Edge cases: special characters, CJK text, AUTOSAR-style tables, etc.
"""

import base64
import tempfile
import unittest
from pathlib import Path

from src.tools.file_ops.markdown_writer import (
    append_markdown_sections,
    write_markdown_file,
    write_markdown_file_raw,
)


class TestWriteMarkdownFile(unittest.TestCase):
    """Tests for write_markdown_file function."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_basic_sections(self):
        """Test creating a basic Markdown file with sections."""
        file_path = str(self.test_dir / "basic.md")
        sections = [
            {"heading": "Introduction", "level": 2, "body": "This is the intro."},
            {"heading": "Details", "level": 2, "body": "Some details here."},
        ]
        result = write_markdown_file(file_path, sections, title="Test Report")
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# Test Report", content)
        self.assertIn("## Introduction", content)
        self.assertIn("This is the intro.", content)
        self.assertIn("## Details", content)

    def test_with_metadata(self):
        """Test metadata block rendering."""
        file_path = str(self.test_dir / "meta.md")
        sections = [{"heading": "Body", "body": "Content here."}]
        metadata = {"Author": "AI Agent", "Date": "2026-03-08", "Project": "vcos1.0"}
        result = write_markdown_file(
            file_path, sections, title="Report", metadata=metadata
        )
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("> **Author**: AI Agent", content)
        self.assertIn("> **Date**: 2026-03-08", content)
        self.assertIn("---", content)

    def test_markdown_table_in_body(self):
        """Test that Markdown tables are correctly written (AUTOSAR-style)."""
        file_path = str(self.test_dir / "table.md")
        table_body = (
            "| File | Module | Type | Responsibility |\n"
            "|------|--------|------|----------------|\n"
            "| CanIf.c | CanIf | c | Main source file |\n"
            "| CanIf.h | CanIf | h | Public API header |\n"
            "| CanSM.c | CanSM | c | State machine impl |"
        )
        sections = [{"heading": "File List", "level": 2, "body": table_body}]
        result = write_markdown_file(file_path, sections, title="CAN Stack File Scan")
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("| CanIf.c |", content)
        self.assertIn("|------|", content)

    def test_chinese_characters(self):
        """Test CJK characters that commonly appear in AUTOSAR analysis reports."""
        file_path = str(self.test_dir / "chinese.md")
        sections = [
            {
                "heading": "CAN \u901a\u4fe1\u6808 \u2014 \u5178\u578b\u8fd0\u884c\u573a\u666f\u4e0e\u8865\u5145\u4fe1\u606f\u9700\u6c42\u62a5\u544a",
                "level": 1,
                "body": "CAN \u529f\u80fd\u6808\u8d1f\u8d23\u6574\u8f66 CAN \u603b\u7ebf\u901a\u4fe1\u7ba1\u7406\uff0c\u5177\u6709\u9ad8\u5b9e\u65f6\u6027\u3001\u591a\u6838\u5e76\u53d1\u8bbf\u95ee\u3002",
            },
            {
                "heading": "\u4f9d\u8d56\u5173\u7cfb\u56fe",
                "level": 2,
                "body": "CanDrv ISR \u2192 CanIf_RxIndication \u2192 PduR/UpperLayer",
            },
        ]
        result = write_markdown_file(file_path, sections)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("\u901a\u4fe1\u6808", content)
        self.assertIn("\u9ad8\u5b9e\u65f6\u6027", content)
        self.assertIn("\u2192", content)

    def test_special_characters_in_body(self):
        """Test special chars that typically break Python string literals."""
        file_path = str(self.test_dir / "special.md")
        body_with_specials = (
            "**Init\u5e8f\u5217\u7ea6\u675f\uff08\u57fa\u4e8eAUTOSAR\u89c4\u8303\uff09\u3010\u63a8\u65ad\u3011**\uff1a\n\n"
            "    EcuM\u542f\u52a8 --> CanIf_Init --> CanSM_Init\n\n"
            "Quotes: 'single' \"double\" `backtick`\n"
            "Path: C:\\Users\\test\\file.c\n"
            "Unicode: \u2705 \u274c \u26a0\ufe0f"
        )
        sections = [{"heading": "Special Chars", "body": body_with_specials}]
        result = write_markdown_file(file_path, sections)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("\u3010\u63a8\u65ad\u3011", content)
        self.assertIn("'single'", content)
        self.assertIn('"double"', content)
        self.assertIn("\u2705", content)

    def test_heading_levels(self):
        """Test various heading levels from 1 to 6."""
        file_path = str(self.test_dir / "levels.md")
        sections = [
            {"heading": "H1", "level": 1, "body": "Level 1"},
            {"heading": "H2", "level": 2, "body": "Level 2"},
            {"heading": "H3", "level": 3, "body": "Level 3"},
            {"heading": "H4", "level": 4, "body": "Level 4"},
            {"heading": "H5", "level": 5, "body": "Level 5"},
            {"heading": "H6", "level": 6, "body": "Level 6"},
        ]
        result = write_markdown_file(file_path, sections)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# H1", content)
        self.assertIn("## H2", content)
        self.assertIn("###### H6", content)

    def test_invalid_heading_level_defaults_to_2(self):
        """Test that invalid heading levels default to 2."""
        file_path = str(self.test_dir / "invalid_level.md")
        sections = [
            {"heading": "Bad Level", "level": 99, "body": "Content"},
            {"heading": "Negative", "level": -1, "body": "Content"},
        ]
        result = write_markdown_file(file_path, sections)
        content = Path(file_path).read_text(encoding="utf-8")
        self.assertEqual(content.count("## "), 2)

    def test_overwrite_false_raises(self):
        """Test that overwrite=False raises when file exists."""
        file_path = str(self.test_dir / "existing.md")
        Path(file_path).write_text("existing content")

        with self.assertRaises(FileExistsError):
            write_markdown_file(
                file_path,
                [{"heading": "New", "body": "new"}],
                overwrite=False,
            )

    def test_overwrite_true_replaces(self):
        """Test that overwrite=True replaces existing content."""
        file_path = str(self.test_dir / "replace.md")
        Path(file_path).write_text("old content")

        write_markdown_file(
            file_path,
            [{"heading": "New", "body": "replaced"}],
            overwrite=True,
        )
        content = Path(file_path).read_text(encoding="utf-8")
        self.assertNotIn("old content", content)
        self.assertIn("replaced", content)

    def test_creates_parent_directories(self):
        """Test automatic parent directory creation."""
        file_path = str(self.test_dir / "deep" / "nested" / "dir" / "report.md")
        sections = [{"heading": "Test", "body": "Content"}]
        result = write_markdown_file(file_path, sections)
        self.assertIn("Successfully wrote", result)
        self.assertTrue(Path(file_path).exists())

    def test_empty_file_path_raises(self):
        """Test that empty file path raises ValueError."""
        with self.assertRaises(ValueError):
            write_markdown_file("", [{"heading": "X", "body": "Y"}])

    def test_empty_sections_raises(self):
        """Test that empty sections list raises ValueError."""
        file_path = str(self.test_dir / "empty.md")
        with self.assertRaises(ValueError):
            write_markdown_file(file_path, [])

    def test_invalid_section_type_raises(self):
        """Test that non-dict section raises ValueError."""
        file_path = str(self.test_dir / "bad.md")
        with self.assertRaises(ValueError):
            write_markdown_file(file_path, ["not a dict"])

    def test_section_without_heading(self):
        """Test section with body only (no heading)."""
        file_path = str(self.test_dir / "no_heading.md")
        sections = [{"body": "Just a paragraph with no heading."}]
        result = write_markdown_file(file_path, sections)
        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("Just a paragraph", content)
        # No heading marker should be present
        self.assertNotIn("#", content)

    def test_section_without_body(self):
        """Test section with heading only (no body)."""
        file_path = str(self.test_dir / "no_body.md")
        sections = [{"heading": "Empty Section", "level": 2}]
        result = write_markdown_file(file_path, sections)
        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("## Empty Section", content)

    def test_large_autosar_report(self):
        """Test a realistic large AUTOSAR analysis report structure."""
        file_path = str(self.test_dir / "CAN_FileScan.md")
        sections = [
            {
                "heading": "\u6587\u4ef6\u6e05\u5355\u4e0e\u89d2\u8272\u5f52\u7c7b",
                "level": 2,
                "body": (
                    "| \u6587\u4ef6 | \u6a21\u5757 | \u7c7b\u578b | \u804c\u8d23 |\n"
                    "|------|------|------|------|\n"
                    "| CanIf.c | CanIf | c | \u4e3b\u6e90\u6587\u4ef6 |\n"
                    "| CanSM.c | CanSM | c | \u72b6\u6001\u673a\u5b9e\u73b0 |"
                ),
            },
            {
                "heading": "\u4f9d\u8d56\u5173\u7cfb\u56fe",
                "level": 2,
                "body": (
                    "### Include \u4f9d\u8d56\n\n"
                    "```\n"
                    "CanIf.c -> CanIf_Can.h, CanIf.h, CanIf_internal.h\n"
                    "```\n\n"
                    "### \u8c03\u7528\u94fe\u8def\n\n"
                    "- `CanDrv ISR -> CanIf_RxIndication -> PduR`\n"
                    "- `ComM/BswM -> CanSM_MainFunction -> CanIf`"
                ),
            },
            {
                "heading": "\u5171\u4eab\u53d8\u91cf\u5019\u9009\u70b9\u4f4d",
                "level": 2,
                "body": (
                    "- `CanIf.c`: CanIf_Global, CanIf_InitStatus[coreID]\n"
                    "- `CanSM_Internal.c`: NetworkState, SpinLock\n"
                    "- `PduR_Spinlock.c`: PduR\u4e13\u7528\u81ea\u65cb\u9501"
                ),
            },
        ]
        metadata = {
            "\u751f\u6210\u65f6\u95f4": "\u81ea\u52a8\u626b\u63cf",
            "\u9879\u76ee": "vcos1.0 AUTOSAR CP BSW",
        }
        result = write_markdown_file(
            file_path,
            sections,
            title="CAN \u901a\u4fe1\u6808 \u2014 \u6587\u4ef6\u626b\u63cf\u62a5\u544a",
            metadata=metadata,
        )
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# CAN", content)
        self.assertIn("> **\u9879\u76ee**: vcos1.0", content)
        self.assertIn("## \u6587\u4ef6\u6e05\u5355", content)
        self.assertIn("| CanIf.c |", content)
        self.assertIn("## \u4f9d\u8d56\u5173\u7cfb\u56fe", content)
        self.assertIn("## \u5171\u4eab\u53d8\u91cf", content)
        file_size = Path(file_path).stat().st_size
        self.assertGreater(file_size, 300)


class TestWriteMarkdownFileRaw(unittest.TestCase):
    """Tests for write_markdown_file_raw function."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_base64_content(self):
        """Test writing from base64-encoded content."""
        file_path = str(self.test_dir / "b64.md")
        original = "# Hello World\n\nThis is a **test**.\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        b64 = base64.b64encode(original.encode("utf-8")).decode("ascii")
        result = write_markdown_file_raw(file_path, content_b64=b64)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertEqual(content, original)

    def test_base64_chinese_content(self):
        """Test base64 with Chinese characters - the exact scenario that fails with ast.parse."""
        file_path = str(self.test_dir / "b64_cn.md")
        original = "# CAN \u901a\u4fe1\u6808 \u2014 \u5178\u578b\u8fd0\u884c\u573a\u666f\u4e0e\u8865\u5145\u4fe1\u606f\u9700\u6c42\u62a5\u544a\n\n> \u751f\u6210\u65f6\u95f4: \u81ea\u52a8\u626b\u63cf"
        b64 = base64.b64encode(original.encode("utf-8")).decode("ascii")
        result = write_markdown_file_raw(file_path, content_b64=b64)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertEqual(content, original)

    def test_plain_content(self):
        """Test writing from plain text content."""
        file_path = str(self.test_dir / "plain.md")
        result = write_markdown_file_raw(
            file_path, content_plain="# Simple\n\nJust text."
        )
        self.assertIn("Successfully wrote", result)
        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# Simple", content)

    def test_base64_takes_priority(self):
        """Test that base64 content takes priority over plain text."""
        file_path = str(self.test_dir / "priority.md")
        b64 = base64.b64encode(b"B64 wins").decode("ascii")
        result = write_markdown_file_raw(
            file_path, content_b64=b64, content_plain="Plain loses"
        )
        content = Path(file_path).read_text(encoding="utf-8")
        self.assertEqual(content, "B64 wins")

    def test_both_empty_raises(self):
        """Test that providing neither content raises ValueError."""
        file_path = str(self.test_dir / "empty.md")
        with self.assertRaises(ValueError):
            write_markdown_file_raw(file_path)

    def test_invalid_base64_raises(self):
        """Test that invalid base64 raises ValueError."""
        file_path = str(self.test_dir / "bad_b64.md")
        with self.assertRaises(ValueError):
            write_markdown_file_raw(file_path, content_b64="not-valid-base64!!!")

    def test_empty_path_raises(self):
        """Test empty path raises ValueError."""
        with self.assertRaises(ValueError):
            write_markdown_file_raw("", content_plain="test")

    def test_overwrite_false_raises(self):
        """Test overwrite=False raises for existing file."""
        file_path = str(self.test_dir / "exist_raw.md")
        Path(file_path).write_text("existing")
        with self.assertRaises(FileExistsError):
            write_markdown_file_raw(
                file_path, content_plain="new", overwrite=False
            )


class TestAppendMarkdownSections(unittest.TestCase):
    """Tests for append_markdown_sections function."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_append_to_existing(self):
        """Test appending sections to an existing file."""
        file_path = str(self.test_dir / "append.md")
        Path(file_path).write_text(
            "# Existing\n\nOriginal content.\n", encoding="utf-8"
        )

        result = append_markdown_sections(
            file_path,
            [{"heading": "New Section", "level": 2, "body": "Appended content."}],
        )
        self.assertIn("Successfully appended", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# Existing", content)
        self.assertIn("Original content.", content)
        self.assertIn("## New Section", content)
        self.assertIn("Appended content.", content)

    def test_append_multiple_sections(self):
        """Test appending multiple sections at once."""
        file_path = str(self.test_dir / "multi.md")
        Path(file_path).write_text("# Start\n", encoding="utf-8")

        sections = [
            {"heading": "Section A", "level": 2, "body": "Content A"},
            {"heading": "Section B", "level": 2, "body": "Content B"},
            {"heading": "Section C", "level": 3, "body": "Content C"},
        ]
        result = append_markdown_sections(file_path, sections)
        self.assertIn("Sections added: 3", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("## Section A", content)
        self.assertIn("## Section B", content)
        self.assertIn("### Section C", content)

    def test_append_to_nonexistent_raises(self):
        """Test that appending to non-existent file raises FileNotFoundError."""
        file_path = str(self.test_dir / "ghost.md")
        with self.assertRaises(FileNotFoundError):
            append_markdown_sections(file_path, [{"heading": "X", "body": "Y"}])

    def test_append_empty_sections_raises(self):
        """Test that empty sections raises ValueError."""
        file_path = str(self.test_dir / "empty_append.md")
        Path(file_path).write_text("content")
        with self.assertRaises(ValueError):
            append_markdown_sections(file_path, [])

    def test_incremental_report_building(self):
        """Test building a report incrementally (simulates multi-step LLM)."""
        file_path = str(self.test_dir / "incremental.md")

        # Step 1: Create initial file
        write_markdown_file(
            file_path,
            [{"heading": "File Scan", "level": 2, "body": "Initial scan results."}],
            title="CAN Stack Analysis",
        )

        # Step 2: Append dependency analysis
        append_markdown_sections(
            file_path,
            [{"heading": "Dependencies", "level": 2, "body": "CanIf -> PduR -> Com"}],
        )

        # Step 3: Append scenario analysis
        append_markdown_sections(
            file_path,
            [
                {
                    "heading": "Scenarios",
                    "level": 2,
                    "body": "Init/MainFunction/ISR paths.",
                },
                {
                    "heading": "Supplementary Info",
                    "level": 2,
                    "body": "Need OS task list.",
                },
            ],
        )

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# CAN Stack Analysis", content)
        self.assertIn("## File Scan", content)
        self.assertIn("## Dependencies", content)
        self.assertIn("## Scenarios", content)
        self.assertIn("## Supplementary Info", content)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases for all markdown writer functions."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_write_to_directory_raises(self):
        """Test writing to a directory path raises ValueError."""
        with self.assertRaises(ValueError):
            write_markdown_file(
                str(self.test_dir),
                [{"heading": "X", "body": "Y"}],
            )

    def test_raw_to_directory_raises(self):
        """Test raw writing to a directory path raises ValueError."""
        with self.assertRaises(ValueError):
            write_markdown_file_raw(str(self.test_dir), content_plain="test")

    def test_whitespace_only_path_raises(self):
        """Test whitespace-only path raises ValueError."""
        with self.assertRaises(ValueError):
            write_markdown_file("   ", [{"heading": "X", "body": "Y"}])

    def test_code_block_in_body(self):
        """Test Markdown code blocks are preserved correctly."""
        file_path = str(self.test_dir / "codeblock.md")
        body = (
            "Example code:\n\n"
            "```c\n"
            "void CanIf_Init(const CanIf_ConfigType* ConfigPtr) {\n"
            "    /* Initialize CanIf */\n"
            "}\n"
            "```"
        )
        sections = [{"heading": "Code Example", "level": 2, "body": body}]
        result = write_markdown_file(file_path, sections)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("```c", content)
        self.assertIn("void CanIf_Init", content)
        self.assertIn("```", content)

    def test_mermaid_diagram_in_body(self):
        """Test Mermaid diagram syntax is preserved."""
        file_path = str(self.test_dir / "mermaid.md")
        body = (
            "```mermaid\n"
            "graph TD\n"
            "    A[CanDrv ISR] --> B[CanIf_RxIndication]\n"
            "    B --> C[PduR_CanIfRxIndication]\n"
            "    C --> D[Com_RxIndication]\n"
            "```"
        )
        sections = [{"heading": "Call Flow", "level": 2, "body": body}]
        result = write_markdown_file(file_path, sections)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("```mermaid", content)
        self.assertIn("graph TD", content)
        self.assertIn("A[CanDrv ISR]", content)


class TestWriteMarkdownTypeCoercion(unittest.TestCase):
    """Tests for automatic JSON string -> native type coercion."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_sections_as_json_string(self):
        """sections passed as JSON string should be auto-coerced to list."""
        import json
        file_path = str(self.test_dir / "coerced.md")
        sections_list = [
            {"heading": "Section 1", "level": 2, "body": "Content A"},
            {"heading": "Section 2", "level": 2, "body": "Content B"},
        ]
        # Simulate LLM passing sections as a JSON string
        result = write_markdown_file(file_path, json.dumps(sections_list))
        self.assertIn("coerced.md", result)
        content = Path(file_path).read_text()
        self.assertIn("Section 1", content)
        self.assertIn("Content B", content)

    def test_metadata_as_json_string(self):
        """metadata passed as JSON string should be auto-coerced to dict."""
        import json
        file_path = str(self.test_dir / "meta_coerced.md")
        sections = [{"heading": "Test", "body": "body text"}]
        metadata = {"author": "AI Agent", "date": "2026-04-15"}
        result = write_markdown_file(
            file_path, sections, metadata=json.dumps(metadata)
        )
        self.assertIn("meta_coerced.md", result)
        content = Path(file_path).read_text()
        self.assertIn("AI Agent", content)

    def test_invalid_sections_string_rejected(self):
        """Non-JSON string for sections should raise ValueError."""
        file_path = str(self.test_dir / "bad.md")
        with self.assertRaises(ValueError) as ctx:
            write_markdown_file(file_path, "not valid json at all")
        self.assertIn("could not be parsed as JSON", str(ctx.exception))

    def test_invalid_metadata_string_rejected(self):
        """Non-JSON string for metadata should raise ValueError."""
        file_path = str(self.test_dir / "bad_meta.md")
        sections = [{"heading": "Test", "body": "ok"}]
        with self.assertRaises(ValueError) as ctx:
            write_markdown_file(file_path, sections, metadata="not json")
        self.assertIn("could not be parsed as JSON", str(ctx.exception))

    def test_whitespace_in_file_path(self):
        """file_path with whitespace should be stripped."""
        file_path = str(self.test_dir / "stripped.md")
        sections = [{"heading": "Test", "body": "ok"}]
        result = write_markdown_file("  " + file_path + "  ", sections)
        self.assertIn("stripped.md", result)
        content = Path(file_path).read_text()
        self.assertIn("Test", content)


if __name__ == "__main__":
    unittest.main()
