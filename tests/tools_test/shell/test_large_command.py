"""Tests for large command and large output handling.

With the stateless subprocess architecture, the PTY buffer deadlock
issue no longer exists (no PTY involved).  These tests verify:

- Large commands (>4KB) execute successfully without any workaround
- Large output is correctly captured via file FD
- Very large output does not cause memory issues
- Special characters in large commands work correctly
"""

import os
import sys
import tempfile

import pytest

from src.tools.shell.process import ShellProcess


# Unix-only tests (subprocess architecture works on both, but tests use /tmp).
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix-only: tests use Unix-specific paths",
)

TIMEOUT_SECONDS = 15


def _make_shell() -> ShellProcess:
    """Create a persistent shell with a short timeout."""
    return ShellProcess(
        persistent=True,
        timeout=TIMEOUT_SECONDS,
        strip_newlines=False,
        return_err_output=True,
        load_profile=False,
    )


def _generate_large_echo_command(target_bytes: int) -> tuple:
    """Build an ``echo`` command whose total length exceeds *target_bytes*."""
    marker = "LARGE_CMD_MARKER_OK"
    line_payload = "A" * 200
    lines = []
    current_size = 0
    while current_size < target_bytes:
        line = f'echo "{line_payload}"'
        lines.append(line)
        current_size += len(line) + 1
    lines.append(f'echo "{marker}"')
    command = "\n".join(lines)
    return command, marker


@pytest.mark.timeout(TIMEOUT_SECONDS + 10)
def test_persistent_large_echo_command():
    """A >5 KB echo command must execute without hanging."""
    proc = _make_shell()
    try:
        command, marker = _generate_large_echo_command(target_bytes=5000)
        assert len(command) > 4096  # Exceeds old PTY buffer limit

        result = proc.run(command)
        assert marker in result, (
            f"Expected marker '{marker}' not found in output "
            f"(output length={len(result)}, first 300 chars={result[:300]!r})"
        )
    finally:
        proc.cleanup()


@pytest.mark.timeout(TIMEOUT_SECONDS + 10)
def test_persistent_large_heredoc_write():
    """A >10 KB heredoc writing a file must work correctly."""
    proc = _make_shell()

    with tempfile.NamedTemporaryFile(
        prefix="test_heredoc_", suffix=".txt", delete=False
    ) as tmp:
        dest_path = tmp.name

    try:
        marker_line = "=== HEREDOC_END_MARKER ==="
        body_line = "This is line {i} of the generated heredoc content."
        lines = []
        current_size = 0
        i = 0
        while current_size < 10000:
            line = body_line.format(i=i)
            lines.append(line)
            current_size += len(line) + 1
            i += 1
        lines.append(marker_line)
        content = "\n".join(lines)

        command = f"cat > {dest_path} << 'ENDOFTEST'\n{content}\nENDOFTEST"
        assert len(command) > 10000

        proc.run(command)

        assert os.path.exists(dest_path), f"Heredoc file was not created: {dest_path}"
        file_content = open(dest_path).read()
        assert marker_line in file_content
    finally:
        proc.cleanup()
        if os.path.exists(dest_path):
            os.remove(dest_path)


@pytest.mark.timeout(TIMEOUT_SECONDS + 10)
def test_persistent_very_large_command():
    """Stress test: a ~50 KB command must still complete successfully."""
    proc = _make_shell()
    try:
        command, marker = _generate_large_echo_command(target_bytes=50000)
        assert len(command) > 50000

        result = proc.run(command)
        assert marker in result
    finally:
        proc.cleanup()


@pytest.mark.timeout(TIMEOUT_SECONDS + 10)
def test_persistent_small_command_still_works():
    """Regression: small commands must produce correct output."""
    proc = _make_shell()
    try:
        result = proc.run('echo "small_cmd_ok"')
        assert "small_cmd_ok" in result
    finally:
        proc.cleanup()


@pytest.mark.timeout(TIMEOUT_SECONDS + 10)
def test_persistent_large_command_with_special_chars():
    """Large commands containing shell-special characters work correctly."""
    proc = _make_shell()
    try:
        tricky_lines = []
        for i in range(60):
            tricky_lines.append(
                f"echo \"line {i}: single'quote dollar backslash\\\\\""
            )
        tricky_lines.append('echo "SPECIAL_CHARS_OK"')
        command = "\n".join(tricky_lines)

        while len(command) < 5000:
            command = 'echo "padding line"\n' + command

        result = proc.run(command)
        assert "SPECIAL_CHARS_OK" in result
    finally:
        proc.cleanup()


@pytest.mark.timeout(TIMEOUT_SECONDS + 10)
def test_large_output_captured_correctly():
    """Verify that large output (100+ lines) is fully captured."""
    proc = ShellProcess(persistent=False)
    result = proc.run("seq 1 500")
    # Should contain first and last numbers
    assert "1" in result
    assert "500" in result
    lines = result.strip().split("\n")
    assert len(lines) >= 500
