import os
import shutil
import tempfile
import pytest

from src.tools.shell.output_interceptor import OutputInterceptor


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path)


def test_no_truncation(temp_dir):
    interceptor = OutputInterceptor(preview_bytes=100, storage_dir=temp_dir)
    text = "Hello, world!"
    interceptor.write(text)
    result = interceptor.finalize()

    assert result == text
    assert not interceptor.spilled_to_disk


def test_truncation_limits(temp_dir):
    # Budget = 10 (5 head, 5 tail)
    interceptor = OutputInterceptor(preview_bytes=10, storage_dir=temp_dir)
    
    interceptor.write("1234567890abcdef")
    # Total written = 16 bytes.
    # Head should be "12345"
    # Tail should be "bcdef"
    result = interceptor.finalize()

    assert "12345" in result
    assert "bcdef" in result
    assert "67890a" not in result
    assert "bytes omitted" in result
    assert "<system_notice>" in result
    assert "</system_notice>" in result
    assert interceptor.spilled_to_disk

    # Verify the full file was written
    with open(interceptor.artifact_path, "rb") as f:
        full_content = f.read().decode("utf-8")
    assert full_content == "1234567890abcdef"


def test_incremental_writes(temp_dir):
    interceptor = OutputInterceptor(preview_bytes=10, storage_dir=temp_dir)
    # Write byte by byte
    for char in "1234567890abcdef":
        interceptor.write(char)
        
    result = interceptor.finalize()
    assert "12345" == result[:5]
    assert "bcdef" == result[-5:]


def test_multibyte_characters(temp_dir):
    # "你好" is 6 bytes in utf-8
    interceptor = OutputInterceptor(preview_bytes=4, storage_dir=temp_dir)
    interceptor.write("你好，世界") # 15 bytes
    result = interceptor.finalize()

    # The interceptor slices by bytes. "你好，世界" is 15 bytes.
    # Head budget is 2 bytes, tail is 2 bytes.
    # The bytes will be split mid-character. Our `errors="replace"` will convert them to .
    assert "" in result
    assert interceptor.spilled_to_disk

    # The full file however should be completely intact
    with open(interceptor.artifact_path, "rb") as f:
        full_content = f.read().decode("utf-8")
    assert full_content == "你好，世界"
