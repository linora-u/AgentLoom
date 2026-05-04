"""
Regression test: reproduce the EXACT SyntaxError from ai_quality_analysis_demo log.

Log reference:
    .logs/ai_quality_analysis/ai_quality_analysis_demo_20260308_192124.log, lines 10217-10440
    Step 35: LLM generated Python code with a huge string concatenation for
    CAN_Scenarios.md. ast.parse() failed at line 2 with:
        "# CAN 通信栈 — 典型运行场景与补充信息需求报告\\n\\n"     ^
        Error: invalid syntax. Perhaps you forgot a comma?

This test:
1. Reproduces the failure by running ast.parse on the original code pattern.
2. Proves that write_markdown_file() avoids the problem entirely.
3. Proves that write_markdown_file_raw() with base64 also works.
"""

import ast
import base64
import tempfile
import unittest
from pathlib import Path

from src.tools.file_ops.markdown_writer import (
    write_markdown_file,
    write_markdown_file_raw,
    append_markdown_sections,
)


# ---------------------------------------------------------------------------
# The EXACT problematic code pattern from the log (simplified but faithful).
# The LLM generated a huge parenthesized string concatenation, but the log
# shows each table row was on a single logical line that got wrapped.  The
# real issue: the string inside parentheses has implicit concatenation that
# ast.parse chokes on when the lines contain unbalanced quotes / em-dashes.
# ---------------------------------------------------------------------------
ORIGINAL_BROKEN_CODE = '''\
scenarios_content = (
    "# CAN 通信栈 — 典型运行场景与补充信息需求报告\\n\\n"
    "> 生成时间: 自动扫描\\n"
    "> 项目: vcos1.0 AUTOSAR CP BSW\\n"
    "## C. 典型运行场景（用于并发审计的"入口集合"）\\n\\n"
    "| 序号 | 入口函数 | 模块 |\\n"
)
'''
# Note: the line with 用于并发审计的"入口集合" has bare Chinese double quotes
# inside an outer regular double-quoted string — this is what kills ast.parse.


class TestReproduceOriginalFailure(unittest.TestCase):
    """Step 1: Prove the original code pattern fails ast.parse()."""

    def test_original_code_fails_ast_parse(self):
        """The original LLM-generated code pattern fails ast.parse — this is the bug."""
        with self.assertRaises(SyntaxError):
            ast.parse(ORIGINAL_BROKEN_CODE)

    def test_simple_implicit_concat_with_chinese_quotes_fails(self):
        """Even a minimal reproduction with Chinese quotes fails."""
        # Chinese left/right double quotation marks " " inside a "-delimited string
        # The LLM wrote: "...并发审计的"入口集合"..." 
        # Python sees the ASCII " as ending the string, then 入口集合 is invalid syntax
        code = '''x = ("并发审计的"入口集合"")'''
        with self.assertRaises(SyntaxError):
            ast.parse(code)


class TestWriteMarkdownFileSolvesTheProblem(unittest.TestCase):
    """Step 2: Prove write_markdown_file() avoids the SyntaxError entirely."""

    def setUp(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

    def tearDown(self):
        self.test_dir_obj.cleanup()

    def test_exact_log_content_via_structured_sections(self):
        """
        Reproduce the EXACT content from the log using structured sections.
        
        The LLM would now generate simple dict literals instead of a huge
        concatenated string. Dict values are short, so ast.parse succeeds.
        """
        file_path = str(self.test_dir / "CAN_Scenarios.md")

        # ---- This is what the LLM would generate instead ----
        # Each section is a simple dict — no complex string concatenation needed.
        sections = [
            {
                "heading": 'C. \u5178\u578b\u8fd0\u884c\u573a\u666f\uff08\u7528\u4e8e\u5e76\u53d1\u5ba1\u8ba1\u7684\u201c\u5165\u53e3\u96c6\u5408\u201d\uff09',
                "level": 2,
                "body": "",
            },
            {
                "heading": "C.1 \u521d\u59cb\u5316/\u53cd\u521d\u59cb\u5316\uff08Init/DeInit\uff09",
                "level": 3,
                "body": (
                    "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 | \u6a21\u5757 | \u6765\u6e90 | \u6267\u884c\u4e0a\u4e0b\u6587 | \u6d89\u53ca\u5171\u4eab\u72b6\u6001 | \u5e76\u53d1\u98ce\u9669 |\n"
                    "|------|----------|------|------|-----------|-------------|----------|\n"
                    "| 1 | CanIf_Init(ConfigPtr) | CanIf | \u786e\u8ba4 | Task(EcuM\u542f\u52a8\u5e8f\u5217) | CanIf_Global(\u5168\u90e8\u6e05\u96f6), CanIf_InitStatus[coreID], CanIf_ConfigPtr, TxPduBuffer\u6e05\u7a7a, FIFO_Buffer_Info\u6e05\u7a7a | \u591a\u6838Init\u65f6\u5e8f\uff1a\u5404\u6838\u72ec\u7acbInit\u4f46\u5171\u4eabCanIf_Global |\n"
                    "| 2 | CanSM_Init(ConfigPtr) | CanSM | \u786e\u8ba4 | Task(EcuM\u542f\u52a8\u5e8f\u5217) | CanSM_Internal_NetworkType(\u5168\u90e8\u521d\u59cb\u5316), CanSM_External_NetworkType | \u591a\u6838Init\u65f6\u5e8f |\n"
                    "| 3 | PduR_Init(ConfigPtr) | PduR | \u786e\u8ba4 | Task(EcuM\u542f\u52a8\u5e8f\u5217) | PduR\u8def\u7531\u8868, \u7f13\u51b2\u6c60 | Init\u540e\u8def\u7531\u8868\u53ea\u8bfb |\n"
                    "| 4 | Com_Init(ConfigPtr) | Com | \u786e\u8ba4 | Task(EcuM\u542f\u52a8\u5e8f\u5217) | Com\u4fe1\u53f7\u7f13\u51b2, PDU\u7f13\u51b2 | Init\u540e\u4fe1\u53f7\u7f13\u51b2\u53ef\u88ab\u591a\u65b9\u8bbf\u95ee |\n"
                    "| 5 | ComM_Init(ConfigPtr) | ComM | \u786e\u8ba4 | Task(EcuM\u542f\u52a8\u5e8f\u5217) | ComM\u901a\u9053\u72b6\u6001 | \u2014 |\n"
                    "| 6 | BswM_Init(ConfigPtr) | BswM | \u786e\u8ba4 | Task(EcuM\u542f\u52a8\u5e8f\u5217) | BswM\u89c4\u5219/\u52a8\u4f5c\u72b6\u6001 | \u2014 |\n"
                    "| 7 | CanIf_DeInit()\u3010\u63a8\u65ad\u3011| CanIf | \u3010\u63a8\u65ad\u3011| Task | \u540cInit | DeInit\u671f\u95f4\u9700\u786e\u4fdd\u65e0ISR\u56de\u8c03 |"
                ),
            },
            {
                "heading": "C.2 \u5468\u671f\u4e3b\u51fd\u6570\uff08MainFunction/Task \u5468\u671f\u8c03\u7528\uff09",
                "level": 3,
                "body": (
                    "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 | \u6a21\u5757 | \u6765\u6e90 | \u5178\u578b\u5468\u671f | \u6267\u884c\u4e0a\u4e0b\u6587 | \u6d89\u53ca\u5171\u4eab\u72b6\u6001 | \u5e76\u53d1\u98ce\u9669 |\n"
                    "|------|----------|------|------|---------|-----------|-------------|----------|\n"
                    "| 1 | CanSM_Internal_MainFunction() | CanSM | \u786e\u8ba4 | 5-10ms\u3010\u63a8\u65ad\u3011| Task | CanSM_Internal_NetworkType(BsmState, busoffevent, \u5b50\u72b6\u6001, \u8ba1\u65f6\u5668), CanSM_External_NetworkType(requestedMode, currentComMode) | **\u6781\u9ad8**: \u4e0eBusOff ISR\u56de\u8c03\u7ade\u4e89busoffevent |\n"
                    "| 2 | Com_MainFunctionRx()\u3010\u63a8\u65ad\u3011| Com | \u3010\u63a8\u65ad\u3011| 5-10ms\u3010\u63a8\u65ad\u3011| Task | Com Rx\u4fe1\u53f7\u7f13\u51b2 | \u4e0eRxIndication\u56de\u8c03\u7ade\u4e89 |"
                ),
            },
            {
                "heading": "C.3 CAN \u4e2d\u65ad\u56de\u8c03\u94fe\uff08Rx/Tx/BusOff/Wakeup\uff09",
                "level": 3,
                "body": (
                    "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 | \u6a21\u5757 | \u6765\u6e90 | \u89e6\u53d1\u6e90 | \u6267\u884c\u4e0a\u4e0b\u6587 | \u6d89\u53ca\u5171\u4eab\u72b6\u6001 | \u5e76\u53d1\u98ce\u9669 |\n"
                    "|------|----------|------|------|--------|-----------|-------------|----------|\n"
                    "| 1 | CanIf_RxIndication() | CanIf | \u786e\u8ba4 | CAN Driver Rx\u4e2d\u65ad | ISR | CanIf_Global.channelData[].ControllerMode(\u8bfb), .PduMode(\u8bfb) | **\u9ad8**: ISR\u8bfbControllerMode/PduMode\u65f6\u53ef\u80fd\u88abTask\u4fee\u6539 |\n"
                    "| 2 | CanIf_TxConfirmation() | CanIf | \u786e\u8ba4 | CAN Driver Tx\u4e2d\u65ad | ISR | TxPduBuffer[coreID](\u8bfb/\u51fa\u961f), FIFO_Buffer_Info[coreID] | **\u6781\u9ad8**: \u4e0eTransmit(Task)\u5e76\u53d1\u64cd\u4f5cTX\u7f13\u51b2 |\n"
                    "| 3 | CanIf_ControllerBusOff() | CanIf | \u786e\u8ba4 | CAN Driver BusOff\u4e2d\u65ad | ISR | CanIf_Global.channelData[].PduMode --> CanSM_ControllerBusOff --> busoffevent(\u5199) | **\u6781\u9ad8** |"
                ),
            },
            {
                "heading": "C.4 \u7f51\u7edc\u7ba1\u7406/\u6a21\u5f0f\u5207\u6362\uff08ComM/BswM/CanSM \u89e6\u53d1\uff09",
                "level": 3,
                "body": (
                    "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 | \u6a21\u5757 | \u89e6\u53d1\u6e90 | \u6267\u884c\u4e0a\u4e0b\u6587 | \u6d89\u53ca\u5171\u4eab\u72b6\u6001 | \u5e76\u53d1\u98ce\u9669 |\n"
                    "|------|----------|------|--------|-----------|-------------|----------|\n"
                    "| 1 | CanSM_RequestComMode() | CanSM | ComM\u8bf7\u6c42 | Task | CanSM_External_NetworkType.requestedMode(\u5199) | **\u9ad8**: \u4e0eMainFunction\u8bfbrequestedMode\u5e76\u53d1 |\n"
                    "| 2 | CanIf_SetControllerMode() | CanIf | CanSM\u8c03\u7528 | Task | CanIf_Global.channelData[].ControllerMode(\u5199) | **\u9ad8**: \u4e0eISR\u56de\u8c03\u8bfbControllerMode\u5e76\u53d1 |\n"
                    "| 3 | CanIf_SetPduMode() | CanIf | CanSM\u8c03\u7528 | Task | CanIf_Global.channelData[].PduMode(\u5199) | **\u9ad8**: \u4e0eISR\u56de\u8c03\u8bfbPduMode\u5e76\u53d1 |"
                ),
            },
        ]

        metadata = {
            "\u751f\u6210\u65f6\u95f4": "\u81ea\u52a8\u626b\u63cf",
            "\u9879\u76ee": "vcos1.0 AUTOSAR CP BSW",
            "\u8303\u56f4": "CanIf / CanSM / Com / PduR / ComM / BswM",
        }

        result = write_markdown_file(
            file_path,
            sections,
            title="CAN \u901a\u4fe1\u6808 \u2014 \u5178\u578b\u8fd0\u884c\u573a\u666f\u4e0e\u8865\u5145\u4fe1\u606f\u9700\u6c42\u62a5\u544a",
            metadata=metadata,
        )
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")

        # Verify key content from the original log is present
        self.assertIn("# CAN \u901a\u4fe1\u6808", content)
        self.assertIn("\u5178\u578b\u8fd0\u884c\u573a\u666f", content)
        self.assertIn("\u201c\u5165\u53e3\u96c6\u5408\u201d", content)  # The Chinese quotes that broke it
        self.assertIn("CanIf_Init(ConfigPtr)", content)
        self.assertIn("CanSM_Internal_MainFunction()", content)
        self.assertIn("CanIf_RxIndication()", content)
        self.assertIn("CanIf_TxConfirmation()", content)
        self.assertIn("CanIf_ControllerBusOff()", content)
        self.assertIn("CanSM_RequestComMode()", content)
        self.assertIn("**\u6781\u9ad8**", content)
        self.assertIn("\u3010\u63a8\u65ad\u3011", content)

        # Verify the code that calls this tool would pass ast.parse
        tool_call_code = '''
sections = [
    {"heading": "C.1 Init", "level": 3, "body": "| Col1 | Col2 |"},
]
write_markdown_file("test.md", sections, title="Report")
'''
        # This must NOT raise SyntaxError
        ast.parse(tool_call_code)

    def test_exact_log_content_via_base64(self):
        """
        Reproduce the EXACT content via base64 — zero escaping issues.
        """
        file_path = str(self.test_dir / "CAN_Scenarios_b64.md")

        # Build the exact Markdown content that the LLM wanted to write
        original_md = (
            "# CAN \u901a\u4fe1\u6808 \u2014 \u5178\u578b\u8fd0\u884c\u573a\u666f\u4e0e\u8865\u5145\u4fe1\u606f\u9700\u6c42\u62a5\u544a\n\n"
            "> \u751f\u6210\u65f6\u95f4: \u81ea\u52a8\u626b\u63cf\n"
            "> \u9879\u76ee: vcos1.0 AUTOSAR CP BSW\n"
            "> \u8303\u56f4: CanIf / CanSM / Com / PduR / ComM / BswM\n\n"
            "---\n\n"
            '## C. \u5178\u578b\u8fd0\u884c\u573a\u666f\uff08\u7528\u4e8e\u5e76\u53d1\u5ba1\u8ba1\u7684\u201c\u5165\u53e3\u96c6\u5408\u201d\uff09\n\n'
            "### C.1 \u521d\u59cb\u5316/\u53cd\u521d\u59cb\u5316\uff08Init/DeInit\uff09\n\n"
            "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 | \u6a21\u5757 |\n"
            "|------|----------|------|\n"
            "| 1 | CanIf_Init(ConfigPtr) | CanIf |\n"
        )

        # Encode to base64 — this is what the LLM would do
        b64 = base64.b64encode(original_md.encode("utf-8")).decode("ascii")

        # The code that calls this tool is trivially parseable
        tool_call_code = f'''
b64 = "{b64}"
write_markdown_file_raw("test.md", content_b64=b64)
'''
        # Must NOT raise SyntaxError
        ast.parse(tool_call_code)

        # Actually write it
        result = write_markdown_file_raw(file_path, content_b64=b64)
        self.assertIn("Successfully wrote", result)

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertEqual(content, original_md)

    def test_incremental_approach_avoids_long_strings(self):
        """
        Prove that append_markdown_sections allows splitting across steps,
        so each step's code is short and simple — less chance of SyntaxError.
        """
        file_path = str(self.test_dir / "CAN_Scenarios_incremental.md")

        # Step 1: create file with first section (small code, easy to parse)
        write_markdown_file(
            file_path,
            [
                {
                    "heading": "C.1 \u521d\u59cb\u5316/\u53cd\u521d\u59cb\u5316",
                    "level": 3,
                    "body": "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 |\n|------|----------|\n| 1 | CanIf_Init |",
                },
            ],
            title="CAN \u901a\u4fe1\u6808 \u2014 \u5178\u578b\u8fd0\u884c\u573a\u666f\u62a5\u544a",
        )

        # Step 2: append C.2 (separate LLM step, separate code block)
        append_markdown_sections(
            file_path,
            [
                {
                    "heading": "C.2 \u5468\u671f\u4e3b\u51fd\u6570",
                    "level": 3,
                    "body": "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 |\n|------|----------|\n| 1 | CanSM_MainFunction |",
                },
            ],
        )

        # Step 3: append C.3
        append_markdown_sections(
            file_path,
            [
                {
                    "heading": "C.3 CAN \u4e2d\u65ad\u56de\u8c03\u94fe",
                    "level": 3,
                    "body": "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 |\n|------|----------|\n| 1 | CanIf_RxIndication |",
                },
            ],
        )

        # Step 4: append C.4
        append_markdown_sections(
            file_path,
            [
                {
                    "heading": "C.4 \u7f51\u7edc\u7ba1\u7406/\u6a21\u5f0f\u5207\u6362",
                    "level": 3,
                    "body": "| \u5e8f\u53f7 | \u5165\u53e3\u51fd\u6570 |\n|------|----------|\n| 1 | CanSM_RequestComMode |",
                },
            ],
        )

        content = Path(file_path).read_text(encoding="utf-8")
        self.assertIn("# CAN \u901a\u4fe1\u6808", content)
        self.assertIn("### C.1", content)
        self.assertIn("### C.2", content)
        self.assertIn("### C.3", content)
        self.assertIn("### C.4", content)
        self.assertIn("CanIf_Init", content)
        self.assertIn("CanSM_MainFunction", content)
        self.assertIn("CanIf_RxIndication", content)
        self.assertIn("CanSM_RequestComMode", content)


class TestChineseQuotesRootCause(unittest.TestCase):
    """
    Deep-dive: the ROOT CAUSE of the log error was Chinese quotation marks
    "\u201c" and "\u201d" inside a Python double-quoted string.

    The LLM wrote:
        "## C. \u5178\u578b\u8fd0\u884c\u573a\u666f\uff08\u7528\u4e8e\u5e76\u53d1\u5ba1\u8ba1\u7684\u201c\u5165\u53e3\u96c6\u5408\u201d\uff09\\n\\n"

    Python's ast.parse is fine with Unicode \u201c\u201d inside a string, BUT
    the log shows the LLM actually used ASCII double-quotes " " instead of
    Unicode \u201c \u201d, which broke the string delimiter matching.

    write_markdown_file() avoids this because the LLM never has to embed
    such characters inside Python string literals in a long concatenation.
    """

    def test_unicode_curly_quotes_in_dict_value_passes(self):
        """Chinese curly quotes in a dict value are safe because they're short strings."""
        code = '''sections = [{"heading": "\u5178\u578b\u8fd0\u884c\u573a\u666f\uff08\u7528\u4e8e\u5e76\u53d1\u5ba1\u8ba1\u7684\u201c\u5165\u53e3\u96c6\u5408\u201d\uff09", "body": "test"}]'''
        # This should NOT raise - Unicode curly quotes are valid inside Python strings
        ast.parse(code)

    def test_ascii_quotes_in_long_concat_fails(self):
        """ASCII double-quotes inside a double-quoted Python string breaks parsing."""
        # This is what the LLM actually produced (using ASCII " not Unicode \u201c)
        code = '''x = ("用于并发审计的"入口集合"")'''
        with self.assertRaises(SyntaxError):
            ast.parse(code)

    def test_structured_approach_immune_to_quote_type(self):
        """write_markdown_file works regardless of quote type in heading."""
        tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        tmp.close()
        try:
            # Even with the problematic Chinese content including various quotes
            result = write_markdown_file(
                tmp.name,
                [{
                    "heading": 'C. \u5178\u578b\u8fd0\u884c\u573a\u666f\uff08\u7528\u4e8e\u5e76\u53d1\u5ba1\u8ba1\u7684\u201c\u5165\u53e3\u96c6\u5408\u201d\uff09',
                    "level": 2,
                    "body": "content",
                }],
                overwrite=True,
            )
            self.assertIn("Successfully wrote", result)
            content = Path(tmp.name).read_text(encoding="utf-8")
            self.assertIn("\u201c\u5165\u53e3\u96c6\u5408\u201d", content)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
