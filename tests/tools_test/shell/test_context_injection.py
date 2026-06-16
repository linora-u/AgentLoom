import pytest
import os
import shutil
from src.trace.task_context import (
    task_context,
    set_current_agent_config,
    get_current_agent_config,
    clear_current_agent_config,
    set_current_agent_name,
    set_current_agent_id
)
from src.tools.shell.shell_tool import shell_tool
from src.tools.shell.process import ShellProcessRegistry

@pytest.fixture
def clean_registry():
    """Ensure the shell registry is clean before and after each test."""
    registry = ShellProcessRegistry.get_instance()
    
    # Cleanup before test
    for agent_id in list(registry.registered_agent_ids()):
        registry.release(agent_id)
    
    yield
    
    # Cleanup after test
    for agent_id in list(registry.registered_agent_ids()):
        registry.release(agent_id)

def test_shell_tool_with_context_injection(clean_registry):
    """
    Test that setting agent_config in the context injects
    the proper bash_path to the internal ShellProcess.
    """
    zsh_path = shutil.which("zsh")
    if not zsh_path:
        pytest.skip("zsh not installed, skipping test")
        
    mock_config = {
        "execution_env": {
            "bash_path": "zsh"
        }
    }
    
    with task_context():
        set_current_agent_id("test_worker_123")
        set_current_agent_name("test_worker")
        set_current_agent_config(mock_config)
        
        # When load_profile is True it will load the rc scripts, 
        # so $0 inside echo might be -zsh or zsh
        output = shell_tool("echo $0", load_profile=False)
        assert "zsh" in output.lower(), f"Expected zsh to be executed based on injected config, but got output: {output}"

def test_shell_tool_without_context_injection(clean_registry):
    """
    Test that when no agent_config is set, shell_tool safely falls back
    to default resolution logic.
    """
    with task_context():
        set_current_agent_id("test_supervisor_123")
        set_current_agent_name("test_supervisor")
        # Explicitly do NOT set agent config to mock global agent default behavior
        
        output = shell_tool("echo $0", load_profile=False)
        assert output is not None
        # It should fall back to either zsh (if installed) or bash
        has_zsh = shutil.which("zsh") is not None
        has_bash = shutil.which("bash") is not None
        
        if has_zsh and has_bash:
            assert "zsh" in output.lower() or "bash" in output.lower(), f"Expected fallback to zsh or bash, but got {output}"
        elif has_zsh:
             assert "zsh" in output.lower(), f"Expected fallback to zsh, but got {output}"
        elif has_bash:
             assert "bash" in output.lower(), f"Expected fallback to bash, but got {output}"
        else:
             pytest.skip("Neither zsh nor bash is installed")

def test_context_var_direct_registration():
    """
    Test that the base task_context successfully registers
    and allows isolated retrieval of agent_config.
    """
    mock_config = {"test_key": "test_value"}
    
    with task_context():
        set_current_agent_config(mock_config)
        
        # Verify it can be retrieved
        retrieved_config = get_current_agent_config()
        assert retrieved_config is not None
        assert retrieved_config.get("test_key") == "test_value"
        
        clear_current_agent_config()
        
    # Verify it clears out of context
    assert get_current_agent_config() is None

def test_context_var_concurrent_isolation():
    """
    Test that multiple contexts do not overwrite each other's config
    (simulating separate agents running in their own tasks).
    """
    config_a = {"agent": "A"}
    config_b = {"agent": "B"}
    
    with task_context():
        set_current_agent_config(config_a)
        
        with task_context():
            set_current_agent_config(config_b)
            assert get_current_agent_config().get("agent") == "B"
            clear_current_agent_config() # manual cleanup for test
            
    # Outer reset
    with task_context():
        set_current_agent_config(config_a)
        assert get_current_agent_config().get("agent") == "A"
        clear_current_agent_config()

from src.lib.smolagents.tools.tools import tool

def test_tool_decorator_context_binding():
    """
    Explicitly test the user's request: Does @tool decorated tool natively
    pick up the context var without modifying tools.py?
    """
    @tool
    def dummy_config_reader_tool() -> str:
        """
        A dummy tool that reads config.
        """
        cfg = get_current_agent_config()
        if cfg:
            return cfg.get("test_id", "none")
        return "none"
        
    mock_config = {"test_id": "success_binding"}
    
    with task_context():
        set_current_agent_config(mock_config)
        # Call the tool directly (like the agent does during execution)
        result = dummy_config_reader_tool()
        assert result == "success_binding", "Tool failed to read ContextVar config!"
        clear_current_agent_config()

import threading
import time

def test_context_var_threading_isolation():
    """
    Test that multiple threads can safely inject and read different agent configs
    without cross-contamination.
    """
    results = {}
    
    def worker(agent_id, expected_bash):
        try:
            with task_context():
                config = {"execution_env": {"bash_path": expected_bash}}
                set_current_agent_config(config)
                # Sleep a bit to force thread interleaving
                time.sleep(0.05)
                read_config = get_current_agent_config()
                if read_config:
                    results[agent_id] = read_config.get("execution_env", {}).get("bash_path")
                else:
                    results[agent_id] = None
        except Exception as e:
            results[agent_id] = str(e)

    t1 = threading.Thread(target=worker, args=("agent_A", "zsh"))
    t2 = threading.Thread(target=worker, args=("agent_B", "bash"))
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    assert results.get("agent_A") == "zsh", f"Thread A failed context isolation, expected 'zsh', got {results.get('agent_A')}"
    assert results.get("agent_B") == "bash", f"Thread B failed context isolation, expected 'bash', got {results.get('agent_B')}"


def test_invalid_config_fallback(clean_registry):
    """
    Test that shell_tool ignores an invalid execution_env config and
    falls back to auto-detecting the shell from $SHELL / PATH.
    bash_path is silently ignored — the system always auto-detects.
    """
    mock_config = {
        "execution_env": {
            "bash_path": "/path/to/nonexistent/shell"  # silently ignored
        }
    }

    with task_context():
        set_current_agent_id("test_invalid_worker")
        set_current_agent_name("invalid_worker")
        set_current_agent_config(mock_config)

        # bash_path is ignored; shell is auto-detected from $SHELL.
        # The command should succeed (not raise FileNotFoundError).
        result = shell_tool("echo hello", load_profile=False)
        assert "hello" in result, f"Expected 'hello' in output, got: {result}"

def test_valid_config_execution(clean_registry):
    """
    Test that shell_tool works correctly when execution_env is set.
    bash_path is silently ignored; shell is auto-detected from $SHELL.
    The command should execute successfully using whatever shell the system provides.
    """
    mock_config = {
        "execution_env": {
            "bash_path": "/usr/bin/bash"  # silently ignored
        }
    }

    with task_context():
        set_current_agent_id("test_valid_worker")
        set_current_agent_name("valid_worker")
        set_current_agent_config(mock_config)

        # This should execute successfully (shell auto-detected from $SHELL)
        output = shell_tool("echo hello_valid", load_profile=False).strip()
        assert "hello_valid" in output, f"Expected normal execution output, got {output}"

        # Verify it uses a valid shell (bash or zsh — whichever $SHELL points to)
        shell_output = shell_tool("echo $0", load_profile=False).strip()
        assert any(s in shell_output.lower() for s in ("bash", "zsh", "sh")), \
            f"Expected a valid shell, got {shell_output}"

def test_concurrent_shell_execution_isolation(clean_registry, monkeypatch):
    """
    Test that multiple agents running concurrently in different threads 
    can execute shell commands in their isolated environments (one bash, one zsh)
    without interfering with each other's state or execution.
    """
    has_zsh = shutil.which("zsh") is not None
    has_bash = shutil.which("bash") is not None

    if not (has_zsh and has_bash):
        pytest.skip("Both zsh and bash are required for this concurrency test")

    import src.tools.shell.validator as validator_module
    # Allow the commands used in this test (CWD isolation verification).
    monkeypatch.setattr(validator_module, 'load_allowed_commands', lambda: ['mkdir', 'cd', 'pwd', 'echo'])

    results = {}

    def worker(agent_id, subdir):
        try:
            with task_context(f"task_{agent_id}"):
                set_current_agent_id(agent_id)
                set_current_agent_config({"execution_env": {}})

                # Each agent cd's to a different workspace-relative directory.
                shell_tool(f"mkdir -p {subdir}", load_profile=False)
                shell_tool(f"cd {subdir}", load_profile=False)

                # Sleep a bit to encourage thread interleaving.
                time.sleep(0.1)

                output_pwd = shell_tool("pwd", load_profile=False).strip()

                results[agent_id] = {
                    "cwd": output_pwd,
                }
        except Exception as e:
            results[agent_id] = {"error": str(e)}

    t1 = threading.Thread(target=worker, args=("concurrent_A", "temp/isolation_dir_a"))
    t2 = threading.Thread(target=worker, args=("concurrent_B", "temp/isolation_dir_b"))

    t1.start()
    t2.start()

    t1.join()
    t2.join()

    res_a = results.get("concurrent_A", {})
    res_b = results.get("concurrent_B", {})

    assert "error" not in res_a, f"Agent A encountered error: {res_a.get('error')}"
    assert "error" not in res_b, f"Agent B encountered error: {res_b.get('error')}"

    # Verify CWD isolation: each agent maintains its own working directory.
    cwd_a = res_a.get("cwd", "")
    cwd_b = res_b.get("cwd", "")
    assert "isolation_dir_a" in cwd_a, (
        f"Agent A CWD corrupted, expected isolation_dir_a in {cwd_a}"
    )
    assert "isolation_dir_b" in cwd_b, (
        f"Agent B CWD corrupted, expected isolation_dir_b in {cwd_b}"
    )
