"""Shell command security validation.

Detects dangerous patterns in shell commands that could lead to
command injection, privilege escalation, or system damage.
Each check can be individually toggled via YAML config:

    shell_settings:
      security_checks:
        command_substitution: true
        env_injection: true
        control_characters: true
        dangerous_shell_prefix: true
        zsh_dangerous_commands: true
        incomplete_commands: true
        process_substitution: true
        ifs_injection: true
        parameter_expansion: true
        destructive_patterns: true
"""

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional

from agentloom.configuration import C
from agentloom.execution.logging import get_logger
from agentloom.execution.tool_governance.shell.shell_command_ast import analyze_shell_command

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SecurityCheckResult:
    """Result of a security check on a shell command."""
    is_safe: bool
    check_id: str           # e.g. "command_substitution"
    message: str            # human-readable reason
    alternative: str = ""   # suggested alternative approach


@dataclass(frozen=True)
class _StaticShellWord:
    value: str
    tilde_expands: bool
    brace_pattern: Optional[str]
    brace_tilde_expands: bool


_QUOTED_OPEN_BRACE = "\x01"
_QUOTED_CLOSE_BRACE = "\x02"
_QUOTED_COMMA = "\x03"
_QUOTED_DOT = "\x04"
_QUOTED_BRACE_SYNTAX = str.maketrans({
    "{": _QUOTED_OPEN_BRACE,
    "}": _QUOTED_CLOSE_BRACE,
    ",": _QUOTED_COMMA,
    ".": _QUOTED_DOT,
})
_RESTORE_BRACE_SYNTAX = str.maketrans({
    _QUOTED_OPEN_BRACE: "{",
    _QUOTED_CLOSE_BRACE: "}",
    _QUOTED_COMMA: ",",
    _QUOTED_DOT: ".",
})


# ---------------------------------------------------------------------------
# Dangerous pattern constants
# ---------------------------------------------------------------------------

# Command substitution patterns — $(), backticks
# Note: ${} is handled by a separate _check_parameter_expansion check.
_COMMAND_SUBSTITUTION_PATTERNS = [
    (re.compile(r'\$\('), "$() command substitution"),
    (re.compile(r'\$\['), "$[] legacy arithmetic expansion"),
]

# Process substitution — >(), <()
_PROCESS_SUBSTITUTION_PATTERNS = [
    (re.compile(r'<\('), "<() process substitution"),
    (re.compile(r'>\('), ">() process substitution"),
    (re.compile(r'=\('), "=() zsh process substitution"),
]

# Zsh-specific dangerous commands that can bypass security checks
ZSH_DANGEROUS_COMMANDS = frozenset({
    "zmodload", "emulate",
    "sysopen", "sysread", "syswrite", "sysseek",
    "zpty", "ztcp", "zsocket", "mapfile",
    "zf_rm", "zf_mv", "zf_ln", "zf_chmod",
    "zf_chown", "zf_mkdir", "zf_rmdir", "zf_chgrp",
})

# Shell interpreter prefixes — these can execute arbitrary code
DANGEROUS_SHELL_PREFIXES = frozenset({
    "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash",
    "cmd", "powershell", "pwsh",
    "env", "xargs",
    "sudo", "doas", "pkexec",
    "nice", "nohup", "timeout", "stdbuf",
})

# Dangerous environment variables that can hijack execution
_DANGEROUS_ENV_VARS = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "PATH", "IFS",
    "BASH_ENV", "ENV", "PROMPT_COMMAND",
    "PYTHONSTARTUP", "PERL5OPT", "RUBYOPT",
    "BASH_FUNC_",
})

# Safe environment variables that are allowed as command prefixes
SAFE_ENV_VARS = frozenset({
    # Go
    "GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GOFLAGS",
    "GOBIN", "GOPATH", "GOPROXY", "GONOSUMCHECK",
    # Rust
    "RUST_BACKTRACE", "RUST_LOG", "RUSTFLAGS", "CARGO_TARGET_DIR",
    # Python
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "PYTHONHASHSEED", "VIRTUAL_ENV",
    # Node
    "NODE_ENV", "NODE_OPTIONS", "NODE_PATH", "NPM_CONFIG_REGISTRY",
    # Java
    "JAVA_HOME", "JAVA_OPTS", "MAVEN_OPTS", "GRADLE_OPTS",
    # Locale / Terminal
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TZ", "TERM", "COLORTERM",
    "FORCE_COLOR", "NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE",
    # CI / Build
    "CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL",
    "DEBUG", "VERBOSE",
    # Credentials (read-only, commonly set in CI)
    "GH_TOKEN", "GITHUB_TOKEN",
})

# Destructive command patterns
_DESTRUCTIVE_PATTERNS = [
    (re.compile(r'\bgit\s+reset\s+--hard'), "git reset --hard"),
    (re.compile(r'\bgit\s+clean\s+-[a-zA-Z]*f'), "git clean -f"),
    (re.compile(r'\bgit\s+push\s+.*--force'), "git push --force"),
    (re.compile(r'\bgit\s+push\s+.*-f\b'), "git push -f"),
    (re.compile(r'\bmkfs\b'), "mkfs (format filesystem)"),
    (re.compile(r'\bdd\s+if='), "dd (disk write)"),
    (re.compile(r':\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;'), "fork bomb"),
    (re.compile(r'\bchmod\s+777\s+/'), "chmod 777 / (open permissions on root)"),
    (re.compile(r'\btruncate\s+-s\s*0\b'), "truncate -s 0 (empty file)"),
    (re.compile(r'\bDROP\s+(TABLE|DATABASE)\b', re.IGNORECASE), "DROP TABLE/DATABASE"),
    (re.compile(r'\bDELETE\s+FROM\b(?!.*\bWHERE\b)', re.IGNORECASE), "DELETE FROM without WHERE"),
    (re.compile(r'\bterraform\s+destroy\b'), "terraform destroy"),
    (re.compile(r'\bkubectl\s+delete\b'), "kubectl delete"),
]

# Control character pattern (0x00-0x08, 0x0E-0x1F except tab/newline/CR)
_CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x08\x0e-\x1f]')


# ---------------------------------------------------------------------------
# Quote-aware content extraction
# ---------------------------------------------------------------------------

def _extract_unquoted_content(command: str) -> str:
    """Extract content outside of single and double quotes.

    This allows patterns like `echo '$HOME'` to pass ($ is inside quotes),
    while `echo $HOME` is detected ($ is unquoted).
    """
    result = []
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single_quote:
                result.append(ch)
            continue

        if ch == '\\' and not in_single_quote:
            escaped = True
            if not in_double_quote:
                result.append(ch)
            continue

        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if not in_single_quote and not in_double_quote:
            result.append(ch)

    return "".join(result)


def _has_unescaped_backtick(content: str) -> bool:
    """Check for unescaped backtick characters in content."""
    i = 0
    while i < len(content):
        if content[i] == '\\' and i + 1 < len(content):
            i += 2  # skip escaped char
            continue
        if content[i] == '`':
            return True
        i += 1
    return False


# ---------------------------------------------------------------------------
# Individual security checks
# ---------------------------------------------------------------------------

def _check_command_substitution(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect $(), $[] command substitution in unquoted context.

    Quote-aware: single-quoted content is excluded from ``unquoted``,
    so ``echo '$(...)'`` will NOT trigger this check.  This aligns
    with Claude Code's security model which allows single-quoted
    literal command substitution.

    Note: ${} parameter expansion is handled by a separate check
    (_check_parameter_expansion) for clarity.
    """
    # Only check $() and $[] — ${} has its own check
    _CMD_SUB_ONLY = [
        (re.compile(r'\$\('), "$() command substitution"),
        (re.compile(r'\$\['), "$[] legacy arithmetic expansion"),
    ]
    _alt = (
        "Use write_file or edit_file tool for multi-line content "
        "containing backticks or $()"
    )
    for pattern, desc in _CMD_SUB_ONLY:
        if pattern.search(unquoted):
            return SecurityCheckResult(
                is_safe=False,
                check_id="command_substitution",
                message=f"Blocked: {desc} detected in command",
                alternative=_alt,
            )
    # Check backticks separately (need escape-aware check)
    if _has_unescaped_backtick(unquoted):
        return SecurityCheckResult(
            is_safe=False,
            check_id="command_substitution",
            message="Blocked: backtick command substitution detected",
            alternative=_alt,
        )
    return None


def _check_process_substitution(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect <(), >(), =() process substitution."""
    for pattern, desc in _PROCESS_SUBSTITUTION_PATTERNS:
        if pattern.search(unquoted):
            return SecurityCheckResult(
                is_safe=False,
                check_id="process_substitution",
                message=f"Blocked: {desc} detected in command",
                alternative=(
                    "Write intermediate results to a file within "
                    "workspace, then read it"
                ),
            )
    return None


def _check_env_injection(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect dangerous environment variable injection in command prefix."""
    # Match VAR=value at the start of command
    env_assign_pattern = re.compile(r'^(\s*[A-Za-z_]\w*=\S*\s*)+')
    match = env_assign_pattern.match(command.strip())
    if not match:
        return None

    prefix = match.group(0)
    assignments = re.findall(r'([A-Za-z_]\w*)=', prefix)

    for var_name in assignments:
        if var_name in SAFE_ENV_VARS:
            continue
        # Check if it matches a dangerous pattern
        for dangerous in _DANGEROUS_ENV_VARS:
            if var_name == dangerous or var_name.startswith(dangerous):
                return SecurityCheckResult(
                    is_safe=False,
                    check_id="env_injection",
                    message=f"Blocked: dangerous environment variable '{var_name}' in command prefix",
                    alternative=(
                        "Configure environment in agent YAML config, "
                        "not in shell commands"
                    ),
                )
    return None


def _check_ifs_injection(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect IFS variable manipulation."""
    if re.search(r'\bIFS\s*=', unquoted):
        return SecurityCheckResult(
            is_safe=False,
            check_id="ifs_injection",
            message="Blocked: IFS variable manipulation detected",
        )
    return None


def _check_control_characters(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect control characters that could hide malicious content."""
    if _CONTROL_CHAR_PATTERN.search(command):
        return SecurityCheckResult(
            is_safe=False,
            check_id="control_characters",
            message="Blocked: control characters detected in command",
        )
    return None


def _check_incomplete_commands(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect incomplete command fragments that suggest injection attempts."""
    # Check raw command for leading tab BEFORE stripping
    if command and command[0] == '\t':
        return SecurityCheckResult(
            is_safe=False,
            check_id="incomplete_commands",
            message="Blocked: command appears to be an incomplete fragment (starts with tab)",
        )

    trimmed = command.strip()

    # Starts with a flag (no command before it)
    if trimmed.startswith('-'):
        return SecurityCheckResult(
            is_safe=False,
            check_id="incomplete_commands",
            message="Blocked: command appears to be an incomplete fragment (starts with flags)",
        )

    # Starts with an operator (continuation of another command)
    if re.match(r'^\s*(&&|\|\||;|>>?|<)', command):
        return SecurityCheckResult(
            is_safe=False,
            check_id="incomplete_commands",
            message="Blocked: command appears to be a continuation fragment (starts with operator)",
        )

    return None


def _check_dangerous_shell_prefix(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect dangerous shell interpreter invocations."""
    # Strip env var assignments to get the actual command
    stripped = re.sub(r'^(\s*[A-Za-z_]\w*=\S*\s*)+', '', command.strip()).strip()
    if not stripped:
        return None

    first_word = stripped.split()[0] if stripped.split() else ""
    # Normalize: /usr/bin/bash → bash
    base_name = first_word.rsplit('/', 1)[-1] if '/' in first_word else first_word

    if base_name in DANGEROUS_SHELL_PREFIXES:
        return SecurityCheckResult(
            is_safe=False,
            check_id="dangerous_shell_prefix",
            message=f"Blocked: '{base_name}' invocation can execute arbitrary commands",
            alternative=(
                "Run commands directly without sudo or shell wrappers"
            ),
        )
    return None


def _check_zsh_dangerous_commands(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect Zsh-specific dangerous builtin commands."""
    # Extract all command names from the unquoted content
    words = unquoted.split()
    for word in words:
        clean = word.strip(';|&')
        if clean in ZSH_DANGEROUS_COMMANDS:
            return SecurityCheckResult(
                is_safe=False,
                check_id="zsh_dangerous_commands",
                message=f"Blocked: Zsh dangerous command '{clean}' detected",
            )
    return None


def _check_parameter_expansion(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect ${} parameter expansion in unquoted context."""
    if '${' in unquoted:
        return SecurityCheckResult(
            is_safe=False,
            check_id="parameter_expansion",
            message="Blocked: ${} parameter expansion detected in unquoted context",
        )
    return None


def _check_destructive_patterns(command: str, unquoted: str) -> Optional[SecurityCheckResult]:
    """Detect known destructive command patterns."""
    rm_description = _destructive_rm_description(command)
    if rm_description is not None:
        return SecurityCheckResult(
            is_safe=False,
            check_id="destructive_patterns",
            message=f"Blocked: destructive command detected — {rm_description}",
            alternative=(
                "Use targeted operations: rm specific-file, "
                "git revert commit-hash"
            ),
        )
    for pattern, desc in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return SecurityCheckResult(
                is_safe=False,
                check_id="destructive_patterns",
                message=f"Blocked: destructive command detected — {desc}",
                alternative=(
                    "Use targeted operations: rm specific-file, "
                    "git revert commit-hash"
                ),
            )
    return None


def _destructive_rm_description(command: str, *, _depth: int = 0) -> Optional[str]:
    """Return a dangerous rm target from parsed shell command nodes."""
    if _depth > 32:
        return "shell syntax could not be verified"
    try:
        invocations = analyze_shell_command(command, static_commands_only=True).commands
    except (ImportError, RecursionError, ValueError):
        return "shell syntax could not be verified"

    for invocation in invocations:
        raw_words = [invocation.name, *invocation.args]
        if any('$"' in raw_word for raw_word in raw_words):
            return "shell syntax could not be verified"
        decoded_words = [
            _decode_static_shell_word(raw_word)
            for raw_word in raw_words
        ]
        if _is_command_query(decoded_words):
            continue

        description = _destructive_rm_words_description(decoded_words)
        if description is not None:
            return description

        embedded = _embedded_shell_description(decoded_words, _depth=_depth)
        if embedded is not None:
            return embedded
    return None


def _destructive_rm_words_description(words: list[Optional[_StaticShellWord]]) -> Optional[str]:
    saw_rm = False
    for word in words:
        expanded_words = _expand_static_shell_word(word)
        if expanded_words is None:
            return "rm shell word expansion could not be verified"
        for expanded_word in expanded_words:
            if saw_rm:
                description = _dangerous_rm_target_description(expanded_word)
                if description is not None:
                    return description
            if (
                expanded_word is not None
                and expanded_word.value.rsplit("/", 1)[-1] == "rm"
            ):
                saw_rm = True
    return None


def _expand_static_shell_word(
    word: Optional[_StaticShellWord],
) -> Optional[list[Optional[_StaticShellWord]]]:
    if word is None:
        return [None]
    if word.brace_pattern is None:
        return [word]

    pending = [word.brace_pattern]
    expanded: list[Optional[_StaticShellWord]] = []
    examined = 0
    while pending:
        candidate = pending.pop()
        examined += 1
        if examined > 256:
            return None
        expansion = _split_first_brace(candidate)
        if expansion is None:
            literal = candidate.translate(_RESTORE_BRACE_SYNTAX)
            expanded.append(_StaticShellWord(
                value=literal,
                tilde_expands=(
                    word.tilde_expands
                    or (word.brace_tilde_expands and literal.startswith("~"))
                ),
                brace_pattern=None,
                brace_tilde_expands=False,
            ))
            continue
        prefix, alternatives, suffix = expansion
        if examined + len(pending) + len(alternatives) > 256:
            return None
        pending.extend(
            prefix + alternative + suffix
            for alternative in reversed(alternatives)
        )
    return expanded


def _is_command_query(words: list[Optional[_StaticShellWord]]) -> bool:
    """Return whether this invocation is the non-executing command -v/-V form."""
    if not words or words[0] is None or words[0].value.rsplit("/", 1)[-1] != "command":
        return False
    for decoded in words[1:]:
        if decoded is None:
            return False
        word = decoded.value
        if not word.startswith("-") or word == "--":
            return False
        if "v" in word[1:] or "V" in word[1:]:
            return True
    return False


def _dangerous_rm_target_description(target: Optional[_StaticShellWord]) -> Optional[str]:
    if target is None:
        return None
    word = target.value
    if word.startswith("/") and posixpath.normpath(word) in {"/", "//"}:
        return "rm targeting /"
    if target.tilde_expands and word.startswith("~"):
        return "rm targeting home"
    if target.brace_pattern is not None:
        return _brace_expansion_description(
            target.brace_pattern,
            tilde_expands=target.brace_tilde_expands,
        )
    return None


def _embedded_shell_description(
    words: list[Optional[_StaticShellWord]],
    *,
    _depth: int,
) -> Optional[str]:
    """Inspect literal shell programs passed to -c and env -S."""
    if _depth > 32:
        return "shell syntax could not be verified"
    shell_names = {"sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash"}
    seen_shell = False
    active_shell_name: Optional[str] = None
    after_command_option = False
    command_after_terminator = False
    command_option_operands = 0
    shell_option_operands = 0
    env_option_operand_indexes: set[int] = set()
    for index, decoded in enumerate(words):
        if decoded is None:
            continue
        if index in env_option_operand_indexes:
            continue
        name = decoded.value.rsplit("/", 1)[-1]
        if not seen_shell and name in shell_names:
            seen_shell = True
            active_shell_name = name
        if shell_option_operands:
            shell_option_operands -= 1
            continue
        if (
            active_shell_name == "bash"
            and decoded.value in {"--rcfile", "--init-file"}
        ):
            shell_option_operands = 1
            continue
        if after_command_option and command_option_operands:
            command_option_operands -= 1
            continue
        if (
            seen_shell
            and decoded.value.startswith("-")
            and not decoded.value.startswith("--")
            and "c" in decoded.value[1:]
        ):
            after_command_option = True
            if active_shell_name == "bash":
                option_letters = decoded.value[1:]
                command_option_operands += option_letters.count("o")
                command_option_operands += option_letters.count("O")
            continue
        if (
            after_command_option
            and active_shell_name == "bash"
            and decoded.value.startswith(("-", "+"))
            and not decoded.value.startswith("--")
        ):
            option_letters = decoded.value[1:]
            value_options = option_letters.count("o") + option_letters.count("O")
            if value_options:
                command_option_operands += value_options
                continue
        if after_command_option and decoded.value == "--":
            command_after_terminator = True
            continue
        if command_after_terminator:
            return _destructive_rm_description(decoded.value, _depth=_depth + 1)
        if after_command_option and not decoded.value.startswith(("-", "+")):
            return _destructive_rm_description(decoded.value, _depth=_depth + 1)
        if name == "env" and not decoded.value.startswith(("-", "+")):
            env_split = _env_split_program(
                words,
                index + 1,
                option_operand_indexes=env_option_operand_indexes,
            )
            if env_split is not None:
                env_program, next_index = env_split
                if "$" in env_program or "`" in env_program:
                    return "shell syntax could not be verified"
                return _destructive_env_split_description(
                    env_program,
                    trailing_words=words[next_index:],
                    _depth=_depth,
                )
    return None


def _destructive_env_split_description(
    program: str,
    *,
    trailing_words: list[Optional[_StaticShellWord]],
    _depth: int,
) -> Optional[str]:
    if _depth > 32:
        return "shell syntax could not be verified"
    try:
        argv = shlex.split(_decode_env_split_escapes(program), posix=True)
    except ValueError:
        return "shell syntax could not be verified"
    words: list[Optional[_StaticShellWord]] = [
        _StaticShellWord(
            value=argument,
            tilde_expands=False,
            brace_pattern=None,
            brace_tilde_expands=False,
        )
        for argument in argv
    ]
    words.extend(trailing_words)
    description = _destructive_rm_words_description(words)
    if description is not None:
        return description
    return _embedded_shell_description(words, _depth=_depth + 1)


def _env_split_program(
    words: list[Optional[_StaticShellWord]],
    index: int,
    *,
    option_operand_indexes: set[int],
) -> Optional[tuple[str, int]]:
    long_options_with_values = {"--unset", "--chdir", "--argv0"}
    short_options_with_values = {"u", "C", "a", "P"}
    while index < len(words):
        option = words[index]
        if option is None:
            return None
        value = option.value
        if value.startswith("--split-string="):
            return value.split("=", 1)[1], index + 1
        if value == "--split-string":
            script = words[index + 1] if index + 1 < len(words) else None
            if script is not None:
                return script.value, index + 2
            return None
        if value == "--":
            return None
        if value in long_options_with_values:
            if index + 1 < len(words):
                option_operand_indexes.add(index + 1)
            index += 2
            continue
        if value.startswith("--"):
            index += 1
            continue
        if not value.startswith("-") or value == "-":
            return None

        option_index = 1
        consumed_separate_value = False
        while option_index < len(value):
            option_name = value[option_index]
            if option_name == "S":
                suffix = value[option_index + 1:]
                if suffix:
                    return suffix, index + 1
                script = words[index + 1] if index + 1 < len(words) else None
                return (script.value, index + 2) if script is not None else None
            if option_name in short_options_with_values:
                if option_index + 1 == len(value):
                    if index + 1 < len(words):
                        option_operand_indexes.add(index + 1)
                    index += 2
                    consumed_separate_value = True
                break
            option_index += 1
        if consumed_separate_value:
            continue
        index += 1
    return None


def _decode_env_split_escapes(program: str) -> str:
    """Decode the control escapes accepted by env --split-string."""
    escapes = {"_": " ", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    decoded: list[str] = []
    index = 0
    while index < len(program):
        if program[index] != "\\" or index + 1 >= len(program):
            decoded.append(program[index])
            index += 1
            continue
        following = program[index + 1]
        if following == "c":
            break
        decoded.append(escapes.get(following, following))
        index += 2
    return "".join(decoded)


def _brace_expansion_description(word: str, *, tilde_expands: bool) -> Optional[str]:
    """Expand bounded comma braces and reject ambiguous expansion trees."""
    pending = [word]
    examined = 0
    while pending:
        candidate = pending.pop()
        examined += 1
        if examined > 256:
            return "rm target expansion could not be verified"
        expansion = _split_first_brace(candidate)
        if expansion is None:
            literal = candidate.translate(_RESTORE_BRACE_SYNTAX)
            if literal.startswith("/") and posixpath.normpath(literal) in {"/", "//"}:
                return "rm targeting /"
            if tilde_expands and literal.startswith("~"):
                return "rm targeting home"
            continue
        prefix, alternatives, suffix = expansion
        if examined + len(pending) + len(alternatives) > 256:
            return "rm target expansion could not be verified"
        pending.extend(prefix + alternative + suffix for alternative in alternatives)
    return None


def _split_first_brace(word: str) -> Optional[tuple[str, list[str], str]]:
    """Split the first balanced Bash list or sequence brace expression."""
    stack: list[tuple[int, list[int]]] = []
    for index, char in enumerate(word):
        if char == "{":
            stack.append((index, []))
            continue
        if char == "," and stack:
            stack[-1][1].append(index)
            continue
        if char != "}" or not stack:
            continue
        start, comma_indexes = stack.pop()
        if comma_indexes:
            boundaries = [start, *comma_indexes, index]
            alternatives = [
                word[left + 1:right]
                for left, right in zip(boundaries, boundaries[1:])
            ]
            return word[:start], alternatives, word[index + 1:]
        sequence_alternatives = _brace_sequence_alternatives(word[start + 1:index])
        if sequence_alternatives is not None:
            return word[:start], sequence_alternatives, word[index + 1:]
    return None


def _brace_sequence_alternatives(expression: str) -> Optional[list[str]]:
    parts = expression.split("..")
    if len(parts) not in {2, 3} or any(not part for part in parts):
        return None

    start_text, end_text = parts[:2]
    step_text = parts[2] if len(parts) == 3 else None
    if (
        step_text is not None
        and _is_signed_decimal(step_text)
        and len(step_text.lstrip("-+")) > 64
    ):
        return [""] * 257
    if (
        len(start_text) == 1
        and len(end_text) == 1
        and start_text.isalpha()
        and end_text.isalpha()
    ):
        start = ord(start_text)
        end = ord(end_text)
        character_sequence = True
        zero_padded = False
        width = 0
    elif _is_signed_decimal(start_text) and _is_signed_decimal(end_text):
        numeric_parts = [start_text, end_text]
        if step_text is not None:
            numeric_parts.append(step_text)
        if any(len(part.lstrip("-+")) > 64 for part in numeric_parts):
            return [""] * 257
        start = int(start_text)
        end = int(end_text)
        character_sequence = False
        width = max(len(start_text.lstrip("-")), len(end_text.lstrip("-")))
        zero_padded = start_text.lstrip("-").startswith("0") or end_text.lstrip("-").startswith("0")
    else:
        return None

    step = 1
    if step_text is not None:
        if not _is_signed_decimal(step_text) or int(step_text) == 0:
            return None
        step = abs(int(step_text))
    if start > end:
        step = -step

    alternatives: list[str] = []
    current = start
    while len(alternatives) <= 256:
        if (step > 0 and current > end) or (step < 0 and current < end):
            break
        if character_sequence:
            alternatives.append(chr(current))
        elif zero_padded:
            sign = "-" if current < 0 else ""
            alternatives.append(sign + f"{abs(current):0{width}d}")
        else:
            alternatives.append(str(current))
        current += step
    return alternatives


def _is_signed_decimal(value: str) -> bool:
    digits = value[1:] if value.startswith(("-", "+")) else value
    return bool(digits) and digits.isascii() and digits.isdigit()


def _decode_static_shell_word(raw_word: str) -> Optional[_StaticShellWord]:
    """Decode a shell word that contains no runtime expansion."""
    value: list[str] = []
    brace_pattern: list[str] = []
    tilde_expands = False
    brace_tilde_expands = False
    has_unquoted_brace = False
    word_started = False
    tilde_context = True
    index = 0
    while index < len(raw_word):
        char = raw_word[index]
        if char == "'":
            end = raw_word.find("'", index + 1)
            if end < 0:
                return None
            literal = raw_word[index + 1:end]
            value.append(literal)
            brace_pattern.append(literal.translate(_QUOTED_BRACE_SYNTAX))
            word_started = word_started or bool(literal)
            tilde_context = False
            index = end + 1
            continue
        if char == '"':
            quoted, index = _decode_double_quoted(raw_word, index + 1)
            if quoted is None:
                return None
            value.append(quoted)
            brace_pattern.append(quoted.translate(_QUOTED_BRACE_SYNTAX))
            word_started = word_started or bool(quoted)
            tilde_context = False
            continue
        if char == "$" and index + 1 < len(raw_word) and raw_word[index + 1] == "'":
            ansi_quoted, index = _decode_ansi_c_quoted(raw_word, index + 2)
            if ansi_quoted is None:
                return None
            value.append(ansi_quoted)
            brace_pattern.append(ansi_quoted.translate(_QUOTED_BRACE_SYNTAX))
            word_started = word_started or bool(ansi_quoted)
            tilde_context = False
            continue
        if char == "$" and index + 1 < len(raw_word) and raw_word[index + 1] == '"':
            return None
        if char == "\\":
            if index + 1 >= len(raw_word):
                return None
            if raw_word[index + 1] != "\n":
                escaped = raw_word[index + 1]
                value.append(escaped)
                brace_pattern.append(escaped.translate(_QUOTED_BRACE_SYNTAX))
                word_started = True
                tilde_context = False
            index += 2
            continue
        if char in {"$", "`"}:
            return None
        if not word_started and tilde_context and char == "~":
            tilde_expands = True
        if not word_started and tilde_context and char == "{":
            brace_tilde_expands = True
        if char in {"{", "}"}:
            has_unquoted_brace = True
        value.append(char)
        brace_pattern.append(char)
        word_started = True
        tilde_context = False
        index += 1
    return _StaticShellWord(
        value="".join(value),
        tilde_expands=tilde_expands,
        brace_pattern="".join(brace_pattern) if has_unquoted_brace else None,
        brace_tilde_expands=brace_tilde_expands,
    )


def _decode_double_quoted(raw_word: str, index: int) -> tuple[Optional[str], int]:
    value: list[str] = []
    while index < len(raw_word):
        char = raw_word[index]
        if char == '"':
            return "".join(value), index + 1
        if char == "\\" and index + 1 < len(raw_word):
            following = raw_word[index + 1]
            if following in {'$', '`', '"', "\\", "\n"}:
                if following != "\n":
                    value.append(following)
                index += 2
                continue
            value.append("\\")
            index += 1
            continue
        if char in {"$", "`"}:
            return None, len(raw_word)
        value.append(char)
        index += 1
    return None, len(raw_word)


def _decode_ansi_c_quoted(raw_word: str, index: int) -> tuple[Optional[str], int]:
    value: list[str] = []
    truncated = False
    escapes = {
        "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
        "'": "'", '"': '"', "?": "?",
    }
    while index < len(raw_word):
        char = raw_word[index]
        if char == "'":
            return "".join(value), index + 1
        if char != "\\":
            if not truncated:
                value.append(char)
            index += 1
            continue
        if index + 1 >= len(raw_word):
            return None, len(raw_word)
        following = raw_word[index + 1]
        if following in escapes:
            if not truncated:
                value.append(escapes[following])
            index += 2
            continue
        if following == "x":
            digits = _take_digits(raw_word, index + 2, "0123456789abcdefABCDEF", 2)
            if not digits:
                if not truncated:
                    value.append("\\x")
                index += 2
            else:
                decoded = chr(int(digits, 16))
                if decoded == "\0":
                    truncated = True
                elif not truncated:
                    value.append(decoded)
                index += 2 + len(digits)
            continue
        if following in {"u", "U"}:
            limit = 4 if following == "u" else 8
            digits = _take_digits(raw_word, index + 2, "0123456789abcdefABCDEF", limit)
            if not digits:
                if not truncated:
                    value.extend(("\\", following))
                index += 2
            else:
                try:
                    codepoint = int(digits, 16)
                    if not truncated:
                        if codepoint == 0:
                            value.append(raw_word[index:index + 2 + len(digits)])
                        else:
                            value.append(chr(codepoint))
                except ValueError:
                    return None, len(raw_word)
                index += 2 + len(digits)
            continue
        if following == "c" and index + 2 < len(raw_word):
            decoded = chr(ord(raw_word[index + 2]) & 0x1f)
            if decoded == "\0":
                truncated = True
            elif not truncated:
                value.append(decoded)
            index += 3
            continue
        if following in "01234567":
            digits = _take_digits(raw_word, index + 1, "01234567", 3)
            decoded = chr(int(digits, 8))
            if decoded == "\0":
                truncated = True
            elif not truncated:
                value.append(decoded)
            index += 1 + len(digits)
            continue
        if not truncated:
            value.extend(("\\", following))
        index += 2
    return None, len(raw_word)


def _take_digits(value: str, start: int, allowed: str, limit: int) -> str:
    end = start
    while end < len(value) and end - start < limit and value[end] in allowed:
        end += 1
    return value[start:end]


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

# Maps config key → checker function
_SECURITY_CHECKS = {
    "command_substitution": _check_command_substitution,
    "process_substitution": _check_process_substitution,
    "env_injection": _check_env_injection,
    "ifs_injection": _check_ifs_injection,
    "control_characters": _check_control_characters,
    "incomplete_commands": _check_incomplete_commands,
    "dangerous_shell_prefix": _check_dangerous_shell_prefix,
    "zsh_dangerous_commands": _check_zsh_dangerous_commands,
    "parameter_expansion": _check_parameter_expansion,
    "destructive_patterns": _check_destructive_patterns,
}


def _get_shell_config_security(key: str, *, default=None):
    """Read a shell config value, preferring per-agent effective config."""
    try:
        from agentloom.execution.trace import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if isinstance(agent_cfg, dict):
            shell = agent_cfg.get("shell_settings")
            if isinstance(shell, dict) and key in shell:
                return shell[key]
    except Exception:
        pass
    return C.get_nested("shell_settings", key, default=default)


def _load_enabled_checks() -> dict:
    """Load security check toggles from config.

    Returns dict of check_id → bool.  Missing keys default to True (enabled).
    Reads from the per-agent effective config first, then falls back
    to the global config singleton.
    """
    raw = _get_shell_config_security("security_checks", default=None)
    if raw is None or not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_command_security(command: str) -> List[SecurityCheckResult]:
    """Run all enabled security checks against a shell command.

    Returns a list of failed checks (empty list = all checks passed).
    Each check can be toggled via ``tools.shell.security_checks.<id>``
    in ``config/system.yaml``.

    Args:
        command: The raw shell command string.

    Returns:
        List of SecurityCheckResult for each failed check.
    """
    if not command or not command.strip():
        return []

    enabled_overrides = _load_enabled_checks()
    unquoted = _extract_unquoted_content(command)

    failures: List[SecurityCheckResult] = []
    for check_id, checker_fn in _SECURITY_CHECKS.items():
        # Check if this specific check is disabled in config
        if not enabled_overrides.get(check_id, True):
            continue

        result = checker_fn(command, unquoted)
        if result is not None:
            failures.append(result)
            logger.info(
                "Security check '%s' blocked command: %s",
                check_id, result.message,
            )
            # Write to per-agent shell audit log
            try:
                from agentloom.execution.tool_governance.shell.audit import get_shell_audit_logger
                audit = get_shell_audit_logger()
                audit.log_security_block(
                    command=command,
                    check_id=check_id,
                    message=result.message,
                )
            except Exception:
                pass  # Never let audit logging break the security pipeline

    return failures


def validate_command_security(command: str) -> None:
    """Validate a command and raise ValueError if any security check fails.

    This is the main entry point used by the shell validator pipeline.

    Args:
        command: The raw shell command string.

    Raises:
        ValueError: If any security check fails.
    """
    failures = check_command_security(command)
    if failures:
        # Report the first failure (most critical)
        msg = failures[0].message
        if failures[0].alternative:
            msg = f"{msg}\nSuggested alternative: {failures[0].alternative}"
        raise ValueError(msg)
