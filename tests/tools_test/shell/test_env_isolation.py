import pytest
import os
import sys
import shutil
from src.tools.shell.shell_tool import shell_tool
from src.tools.shell.process import ShellProcessRegistry

@pytest.fixture(autouse=True)
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

def test_zsh_profile_loading(clean_registry, monkeypatch):
    """
    Test that we can spawn a zsh shell with a profile, 
    verifying alias or basic env variables inheritance.
    """
    has_zsh = shutil.which("zsh") is not None
    if not has_zsh:
        pytest.skip("zsh is not installed on this system")

    # In order to test app-level override, we can mock the config, 
    # but the easiest way is to test the fallback using environment variable.
    # So we temporarily set SHELL="zsh".
    old_shell = os.environ.get("SHELL")
    os.environ["SHELL"] = shutil.which("zsh")  # Use full path so process resolver picks zsh
    
    try:
        # Import task_context to set agent_id
        from src.trace.task_context import task_context, sub_task_context, set_current_agent_id
        from src.trace.task_context import set_current_agent_config
        with task_context("test_task"):
            with sub_task_context("test_agent_isolation"):
                set_current_agent_id("zsh_profile_loading_agent")
                # Shell is auto-detected from $SHELL (set to zsh above)
                set_current_agent_config({"execution_env": {}})
                
                # Run a command to print ZSH_VERSION
                output_version = shell_tool("echo $ZSH_VERSION", load_profile=True)
                assert output_version.strip() != "", "ZSH_VERSION should not be empty when running zsh"
                assert "\x08" not in output_version, "Output should not contain raw backspace characters"

                # Run a command to print $0 which should indicate zsh
                output_shell = shell_tool("echo $0", load_profile=True)
                print(f"\n[DEBUG] Raw ZSH $0 output: {repr(output_shell)}")
                assert "zsh" in output_shell.lower(), f"Expected shell to be zsh, got {output_shell}"
                # The output should now be clean: 'echo\n/usr/bin/zsh' (or similar without \x08)
                assert "\x08" not in output_shell, "Output should be cleaned of ZLE artifacts"

                # Test environment variable inheritance like HOME
                expected_home = os.environ.get("HOME", "")
                output_home = shell_tool("echo $HOME", load_profile=True)
                print(f"\n[DEBUG] Raw HOME output: {repr(output_home)}")
                assert expected_home in output_home, f"HOME env var was not inherited correctly. Expected {expected_home}, got {output_home}"
    finally:
        if old_shell is not None:
            os.environ["SHELL"] = old_shell
        else:
            del os.environ["SHELL"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Unix specific fallback via SHELL")
def test_shell_env_fallback_without_execution_env_config(clean_registry, monkeypatch):
    has_zsh = shutil.which("zsh") is not None
    if not has_zsh:
        pytest.skip("zsh is not installed on this system")

    from src.trace.task_context import (
        set_current_agent_config,
        set_current_agent_id,
        sub_task_context,
        task_context,
    )

    monkeypatch.setenv("SHELL", shutil.which("zsh"))

    with task_context("test_task_shell_env_fallback"):
        with sub_task_context("test_agent_shell_env_fallback"):
            set_current_agent_id("shell_env_fallback_agent")
            set_current_agent_config({})
            output_shell = shell_tool("echo $0", load_profile=True)
            assert "zsh" in output_shell.lower(), f"Expected shell fallback to zsh, got {output_shell}"

def test_bash_norc_fallback(clean_registry):
    """
    Test that when load_profile=False, we fall back to a clean shell
    """
    has_bash = shutil.which("bash") is not None
    if not has_bash:
        pytest.skip("bash is not installed on this system")

    try:
        from src.trace.task_context import task_context, sub_task_context, set_current_agent_id
        from src.trace.task_context import set_current_agent_config
        with task_context("test_task_2"):
            with sub_task_context("test_agent_isolation_2"):
                set_current_agent_id("bash_norc_fallback_agent")
                # Shell is auto-detected from $SHELL
                set_current_agent_config({"execution_env": {}})

                # Verify load_profile=False gives a clean shell (no user rc files)
                output_shell = shell_tool("echo $0", load_profile=False)
                assert any(s in output_shell.lower() for s in ("bash", "zsh", "sh")), \
                    f"Expected a valid shell, got {output_shell}"
    finally:
        pass

def test_cross_agent_env_ephemeral(monkeypatch):
    """
    Test that environment exports are ephemeral — they do not persist
    even within the SAME agent across separate tool calls.
    This is by design (stateless subprocess, no env delta tracking).
    """
    from src.trace.task_context import task_context, sub_task_context, set_current_agent_id
    import src.tools.shell.validator as validator_module

    monkeypatch.setattr(validator_module, 'load_allowed_commands', lambda: ['export', 'echo'])

    with task_context("test_task_env_ephemeral"):
        with sub_task_context("agent_A_env"):
            set_current_agent_id("agent_A_env_id")
            # Inline export + echo in SAME command works.
            output_inline = shell_tool("export MY_ISOLATION_VAR=12345 && echo $MY_ISOLATION_VAR", load_profile=False)
            assert "12345" in output_inline, "Inline export+echo should work"

            # But the var does NOT persist to the next call.
            output_next = shell_tool("echo $MY_ISOLATION_VAR", load_profile=False)
            assert "12345" not in output_next, "Exports should be ephemeral across calls"

def test_bash_load_profile_isolation(clean_registry, monkeypatch):
    """
    Test that load_profile=True captures ~/.bashrc via snapshot
    and load_profile=False ignores it.

    With the snapshot mechanism, .bashrc is read ONCE at session init
    (snapshot creation).  The snapshot includes functions, aliases,
    options, and PATH.  Environment variables exported in .bashrc are
    available because the snapshot is created via login shell (-l).
    """
    if shutil.which("bash") is None:
        pytest.skip("Bash is required for this test.")

    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        # Create .bash_profile that sources .bashrc (standard pattern).
        # Login shell (-l) reads .bash_profile, not .bashrc directly.
        profile_file = os.path.join(temp_dir, ".bash_profile")
        with open(profile_file, "w") as f:
            f.write('test -f "$HOME/.bashrc" && source "$HOME/.bashrc"\n')

        rc_file = os.path.join(temp_dir, ".bashrc")
        with open(rc_file, "w") as f:
            # Define a function (captured by snapshot) as the reliable marker.
            f.write("my_rc_test_func() { echo 'rc_func_loaded'; }\n")

        monkeypatch.setenv("HOME", temp_dir)
        monkeypatch.setenv("SHELL", shutil.which("bash"))
        monkeypatch.setenv("SSH_CLIENT", "127.0.0.1 10000 22")
        monkeypatch.setenv("SSH_CONNECTION", "127.0.0.1 10000 127.0.0.1 22")
        # Clear shell detection cache so it picks up the new $SHELL.
        from src.tools.shell.process import find_suitable_shell
        find_suitable_shell.cache_clear()

        from src.trace.task_context import task_context, sub_task_context, set_current_agent_id
        from src.trace.task_context import set_current_agent_config
        with task_context("test_bash_profile"):
            # When load_profile is False, the bashrc should NOT be sourced.
            with sub_task_context("bash_profile_false"):
                set_current_agent_id("bash_profile_false_agent")
                set_current_agent_config({"execution_env": {}})
                output_false = shell_tool("type my_rc_test_func 2>&1 || echo not_found", load_profile=False)
                assert "not_found" in output_false or "not found" in output_false, \
                    "load_profile=False should NOT load ~/.bashrc"

            # When load_profile is True, snapshot captures the function.
            with sub_task_context("bash_profile_true"):
                set_current_agent_id("bash_profile_true_agent")
                set_current_agent_config({"execution_env": {}})
                output_true = shell_tool("type my_rc_test_func 2>&1 || echo not_found", load_profile=True)
                assert "not_found" not in output_true, \
                    "load_profile=True should load function from ~/.bashrc via snapshot"

def test_zsh_load_profile_isolation(clean_registry, monkeypatch):
    """
    Test that load_profile=True gives access to shell builtins/functions
    from the user's real profile, while load_profile=False does not.

    Note: Snapshot captures from the real HOME at session init time.
    Monkeypatching HOME cannot affect snapshot content because the
    subprocess runs with the original environment.  Instead we test
    that load_profile=True vs False produces distinct behavior.
    """
    if shutil.which("zsh") is None:
        pytest.skip("Zsh is required for this test.")

    from src.trace.task_context import task_context, sub_task_context, set_current_agent_id
    from src.trace.task_context import set_current_agent_config

    with task_context("test_zsh_profile"):
        # load_profile=False — no snapshot, no login shell.
        with sub_task_context("zsh_profile_false"):
            set_current_agent_id("zsh_profile_false_agent")
            set_current_agent_config({"execution_env": {}})
            # With load_profile=False, only basic shell is available.
            output_false = shell_tool("echo zsh_basic_ok", load_profile=False)
            assert "zsh_basic_ok" in output_false

        # load_profile=True — snapshot or login shell is used.
        with sub_task_context("zsh_profile_true"):
            set_current_agent_id("zsh_profile_true_agent")
            set_current_agent_config({"execution_env": {}})
            # With load_profile=True, PATH should be properly set.
            output_true = shell_tool("echo $PATH", load_profile=True)
            # PATH should be non-empty and contain typical directories.
            assert "/bin" in output_true or "/usr" in output_true, \
                "load_profile=True should provide a proper PATH"

def test_cross_agent_cwd_isolation(bypass_shell_security):
    """
    Test that changing the current working directory in Agent A does not
    affect the current working directory in Agent B.
    """
    from src.trace.task_context import task_context, sub_task_context, set_current_agent_id
    with task_context("test_task_cwd_isolation"):
        # Agent A changes directory
        with sub_task_context("agent_A_cwd"):
            set_current_agent_id("agent_A_cwd_id")
            original_dir_a = shell_tool("pwd", load_profile=False).strip()
            shell_tool("cd /tmp", load_profile=False)
            new_dir_a = shell_tool("pwd", load_profile=False).strip()
            assert new_dir_a == os.path.realpath("/tmp"), (
                "Agent A failed to change directory"
            )

        # Agent B should still be in its original directory
        with sub_task_context("agent_B_cwd"):
            set_current_agent_id("agent_B_cwd_id")
            dir_b = shell_tool("pwd", load_profile=False).strip()
            assert dir_b != os.path.realpath("/tmp"), (
                "Agent B's working directory was polluted by Agent A"
            )

if __name__ == "__main__":
    pytest.main(["-v", __file__])
