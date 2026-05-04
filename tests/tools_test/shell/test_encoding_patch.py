import pytest
import sys
from unittest.mock import patch
import subprocess

from src.tools.shell.shell_tool import shell_tool
import src.tools.shell.validator as validator_module
import src.tools.shell.process as process_module


@pytest.mark.skipif(sys.platform != 'win32', reason="Windows specific test reliant on wexpect")
def test_shell_tool_windows_encoding_patch(monkeypatch):
    """
    Test that on Windows systems, the shell_tool correctly prepends 
    'chcp 65001 >nul &' to commands to enforce UTF-8 output and that 
    the subprocess run and OutputInterceptor pipeline works.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    test_command = "echo hello world"
    
    with patch.object(validator_module, 'analyze_command', return_value=(["echo"], [])):
        with patch.object(validator_module, 'load_allowed_commands', return_value=["echo"]):
            with patch.object(validator_module, 'load_allowed_operators', return_value=[]):
                with patch.object(subprocess, 'run') as mock_subprocess_run:
                    mock_proc = mock_subprocess_run.return_value
                    mock_proc.returncode = 0
                    mock_proc.stdout = "hello world\n"
                    
                    result = shell_tool(test_command)
                    
                    mock_subprocess_run.assert_called_once()
                    executed_command = mock_subprocess_run.call_args[0][0]
                    assert executed_command == "chcp 65001 >nul & echo hello world"
                    
                    assert isinstance(result, str)
                    assert "hello world" in result
    
def test_shell_tool_real_execution(monkeypatch):
    """
    Test the actual end-to-end execution of shell_tool -> OutputInterceptor
    on the native platform without deep mocks on the core tools.
    """
    test_command = "echo 'hello world'"
    with patch.object(validator_module, 'analyze_command', return_value=(["echo"], [])):
        with patch.object(validator_module, 'load_allowed_commands', return_value=["echo"]):
            with patch.object(validator_module, 'load_allowed_operators', return_value=["'"]):
                result = shell_tool(test_command)

    assert "hello world" in result
    assert isinstance(result, str)

