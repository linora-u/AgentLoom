import pytest
from src.tools.shell.ansi_stripper import strip_ansi

def test_strip_ansi_basic():
    # Basic color codes
    input_text = "Hello \x1b[31mRed\x1b[0m World"
    assert strip_ansi(input_text) == "Hello Red World"
    
def test_strip_ansi_complex():
    # Complex formatting with background colors and bold
    input_text = "\x1b[1;32;40mSuccess!\x1b[0m"
    assert strip_ansi(input_text) == "Success!"
    
def test_strip_ansi_cursor_movement():
    # VSCode shell integration style or cursor movements
    input_text = "Prompt> \x1b]633;A\x07Text"
    assert strip_ansi(input_text) == "Prompt> Text"
    
def test_strip_ansi_no_ansi():
    # Plain text
    input_text = "Just some plain text."
    assert strip_ansi(input_text) == input_text

def test_strip_ansi_empty_and_none():
    assert strip_ansi("") == ""
    assert strip_ansi(None) is None
