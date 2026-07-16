from src.lib.logging import get_logger
from src.lib.runtime import get_current_run_context
from src.tools.shell.output_interceptor import OutputInterceptor
from src.tools.shell.process import ShellProcess, ShellProcessRegistry
from src.tools.shell.should_use_sandbox import get_sandbox_manager, should_use_sandbox
from src.tools.shell.validator import validate_command
from src.trace import capture_explicit_execution_context

logger = get_logger(__name__)


def _no_command_message() -> str:
    return "No shell command was provided; nothing was executed. Please provide a non-empty shell command string."


def _no_output_message(command: str) -> str:
    return f"Shell command executed successfully but produced no output.\nExecuted command: {command}"


def shell_tool(
    command: str,
    timeout: int = 120,
    load_profile: bool = True,
    run_in_background: bool = False,
) -> str:
    """Run a shell command in a per-agent shell session.

    Each invocation executes in a fresh subprocess.  Agents still get an
    isolated shell session that preserves the current working directory
    (CWD) across calls and replays a shell snapshot for aliases, functions,
    shell options, and PATH.  Environment variable exports made inside one
    command are intentionally ephemeral; keep assignment and use in the same
    command when a variable is needed.

    Tool Preferences (use dedicated tools when available):
      - Prefer glob_search over ``find`` for file discovery
      - Prefer grep_search over ``grep`` for text search
      - Prefer edit_file over ``sed``/``awk`` for file modifications
      - Prefer read_file over ``cat`` for reading file contents

    Command Best Practices:
      - Chain commands with ``&&`` to ensure sequential success
      - Use ``|`` for piping output between commands
      - Avoid ``;`` unless prior command failures are acceptable
      - For large outputs, pipe through ``head``/``tail``/``grep``

    Git Safety:
      - Never use ``--force`` or ``--force-with-lease`` on shared branches
      - Never use ``--no-verify`` to skip pre-commit hooks
      - Always run ``git diff`` before committing changes
      - Create new commits instead of amending shared history

    Output Handling:
      - Output exceeding 30 KB is automatically truncated (head + tail)
      - Truncated output is saved to disk; path included in response
      - Use ``head -n`` or ``tail -n`` to limit output proactively

    Background Tasks:
      - Set ``run_in_background=True`` to start a command without waiting
      - Commands that exceed their timeout are auto-promoted to background
      - Use ``check_background_task(task_id)`` to monitor progress
      - Use ``kill_background_task(task_id)`` to stop a background task
      - Use ``list_background_tasks()`` to see all background tasks

    Security:
      - Command substitution (``$()``, backticks) is blocked
      - Dangerous env vars (LD_PRELOAD, IFS, PATH override) are blocked
      - Operations outside the project directory are restricted
      - sudo, bash -c, and shell interpreter invocations are blocked
      - Destructive patterns (rm -rf /, git reset --hard) are blocked
      - Sandbox mode available via YAML config for OS-level isolation

    Args:
        command: Shell command string to execute.
        timeout: Maximum execution time in seconds (default 120, max 1800).
        load_profile: Load shell profile (~/.bashrc or ~/.zshrc) before
            executing commands (default True).
        run_in_background: If True, start the command immediately as a
            background task and return the task_id without waiting for
            completion (default False).

    Returns:
        Combined stdout/stderr output.  If the command produces no output,
        returns a guidance message.  Non-zero exit codes for commands like
        grep (1 = no matches) and diff (1 = files differ) are interpreted
        semantically and do not indicate errors.

        When ``run_in_background=True`` or a command is auto-promoted to
        background, the return includes the background task ID and usage
        instructions for monitoring tools.

    Raises:
        ValueError: If the command is blocked by security checks, path
            validation, or the command/operator whitelist.

    Examples:
        shell_tool("echo 'Hello World' && ls -la")
        shell_tool("git status")
        shell_tool("pwd", timeout=60)
        shell_tool("make build", timeout=300)
        shell_tool("npm run dev", run_in_background=True)
    """
    if command is None:
        return _no_command_message()

    if not isinstance(command, str):
        raise ValueError("command must be a non-empty string")

    command = command.strip()
    if not command:
        return _no_command_message()

    try:
        from src.tools.shell.shell_audit_log import get_shell_audit_logger

        get_shell_audit_logger().log_effective_policy()
    except Exception:
        pass

    # Detect trailing & (explicit background request).
    if not run_in_background and command.rstrip().endswith("&"):
        bare = command.rstrip().rstrip("&").rstrip()
        if bare and not bare.endswith("&"):
            # Single trailing & — treat as background request.
            run_in_background = True
            command = bare

    # Clamp timeout to a reasonable maximum (30 minutes)
    timeout = min(timeout, 1800)

    # Resolve agent context early so we can pass the shell session's
    # actual CWD to the path validator.  This closes a security gap
    # where the session CWD diverges from os.getcwd().
    runtime_context = get_current_run_context()
    execution_context = capture_explicit_execution_context()
    agent_id = execution_context.agent_id if runtime_context is not None else None
    session_cwd = None
    if agent_id:
        registry = ShellProcessRegistry.get_instance()
        session_cwd = registry.get_session_cwd(agent_id)

    validate_command(command, cwd=session_cwd)

    # Sandbox wrapping: if enabled, wrap the command with OS-level isolation
    exec_command = command
    if should_use_sandbox(command):
        sandbox_mgr = get_sandbox_manager()
        if sandbox_mgr.is_available():
            exec_command = sandbox_mgr.wrap_command(command)
            logger.info("Command sandboxed via %s", sandbox_mgr.config.mode)
            try:
                from src.tools.shell.shell_audit_log import get_shell_audit_logger

                get_shell_audit_logger().log_sandbox_wrap(
                    command,
                    sandbox_mgr.config.mode,
                )
            except Exception:
                pass
        else:
            reason = sandbox_mgr.get_unavailable_reason()
            logger.warning("Sandbox requested but unavailable: %s", reason)
            try:
                from src.tools.shell.shell_audit_log import get_shell_audit_logger

                get_shell_audit_logger().log_sandbox_unavailable(
                    command,
                    sandbox_mgr.config.mode,
                    reason or "unknown",
                )
            except Exception:
                pass

    # Handle explicit background execution.
    if run_in_background:
        return _run_in_background(exec_command, command, timeout, load_profile)

    # Resolve the shell process to use:
    #   - In an agent context  -> reuse/create the agent's dedicated shell session
    #   - Outside agent context -> create a one-shot subprocess (legacy behaviour)
    if agent_id:
        process = registry.get_or_create(
            agent_id=agent_id,
            timeout=timeout,
            session_scoped=True,
            strip_newlines=False,
            return_err_output=True,
            load_profile=load_profile,
        )
    else:
        process = ShellProcess(
            timeout=timeout,
            session_scoped=False,
            strip_newlines=False,
            return_err_output=True,
            load_profile=load_profile,
        )

    result_text = process.run(exec_command)

    if not result_text:
        return _no_output_message(command)

    interceptor = OutputInterceptor()
    interceptor.write(result_text)
    return interceptor.finalize()


def _run_in_background(
    exec_command: str,
    original_command: str,
    timeout: int,
    load_profile: bool,
) -> str:
    """Spawn a command as a background task without waiting.

    The command starts in a subprocess and is immediately registered
    in the BackgroundTaskRegistry.  Output is written to a durable
    file that can be inspected via check_background_task().
    """
    import os
    import subprocess

    from src.lib.runtime import get_current_run_context
    from src.tools.shell.background_task import BackgroundTaskRegistry
    from src.tools.shell.process import _MAX_OUTPUT_BYTES, find_suitable_shell
    from src.tools.shell.subprocess_env import build_subprocess_env
    from src.tools.shell.tree_kill import SizeWatchdog, graceful_kill

    shell_path = find_suitable_shell()
    env = build_subprocess_env()

    # Build the shell command.
    escaped = exec_command.replace("'", "'\\''")
    shell_args = [shell_path, "-c", f"eval '{escaped}'"]

    # Create durable output file.
    runtime_context = get_current_run_context(required=True)
    assert runtime_context is not None
    out_fd, output_path_obj = runtime_context.allocate_artifact(
        "background",
        prefix="background-",
        suffix=".txt",
    )
    output_path = str(output_path_obj)

    try:
        proc = subprocess.Popen(
            shell_args,
            stdout=out_fd,
            stderr=out_fd,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except BaseException:
        os.close(out_fd)
        try:
            runtime_context.remove_run_file(output_path_obj)
        except FileNotFoundError:
            pass
        raise

    watchdog = None
    try:
        # Start size watchdog and transfer ownership to the registry.
        watchdog = SizeWatchdog(
            proc.pid,
            output_path,
            max_bytes=_MAX_OUTPUT_BYTES,
            output_fd=out_fd,
        )
        watchdog.start()
        registry = BackgroundTaskRegistry.get_instance()
        task_id = registry.register(
            process=proc,
            command=original_command,
            output_path=output_path,
            description=original_command[:80],
            size_watchdog=watchdog,
            output_fd=out_fd,
        )
    except BaseException:
        os.close(out_fd)
        if watchdog is not None:
            watchdog.stop()
        graceful_kill(proc.pid, grace_ms=500)
        try:
            runtime_context.remove_run_file(output_path_obj)
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(out_fd)

    logger.info("Background task %s started: pid=%d", task_id, proc.pid)
    return (
        f"[Background Task: {task_id}]\n"
        f"Command started in background: {original_command[:120]}\n"
        f"PID: {proc.pid}\n\n"
        f"Use check_background_task('{task_id}') to monitor progress.\n"
        f"Use kill_background_task('{task_id}') to stop it.\n"
        f"Use list_background_tasks() to see all background tasks."
    )
