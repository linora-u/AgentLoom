"""Security policy summary for LLM transparency.

Generates structured text describing the active security policy
for embedding in tool descriptions and environment prompts.
Reads from the SAME configuration sources that enforcement uses,
ensuring prompt and enforcement are always in sync (single source
of truth).
"""

from __future__ import annotations

import re
from typing import Dict, List

from src.lib.config import C
from src.lib.logging import get_logger
from src.lib.permissions.workspace import get_allowed_directories

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Human-readable descriptions for each security check ID.
# Keys match the config keys in shell_settings.security_checks.
# ---------------------------------------------------------------------------

SECURITY_CHECK_DESCRIPTIONS: Dict[str, str] = {
    "command_substitution": (
        "command_substitution: $() and backticks are blocked "
        "(including inside heredoc)"
    ),
    "env_injection": (
        "env_injection: LD_PRELOAD, IFS, PATH override are blocked"
    ),
    "control_characters": (
        "control_characters: control characters in commands are blocked"
    ),
    "dangerous_shell_prefix": (
        "dangerous_shell_prefix: sudo, bash -c, shell interpreter "
        "invocations are blocked"
    ),
    "zsh_dangerous_commands": (
        "zsh_dangerous_commands: dangerous zsh-specific commands are blocked"
    ),
    "incomplete_commands": (
        "incomplete_commands: incomplete/truncated commands are blocked"
    ),
    "process_substitution": (
        "process_substitution: <() and >() are blocked"
    ),
    "ifs_injection": (
        "ifs_injection: IFS injection patterns are blocked"
    ),
    "parameter_expansion": (
        "parameter_expansion: dangerous parameter expansion patterns "
        "are blocked"
    ),
    "destructive_patterns": (
        "destructive_patterns: rm -rf /, format, mkfs patterns are blocked"
    ),
}


DENIAL_BEHAVIOR_TEXT = """\
When a command is blocked by security:
  - Do NOT retry the same command
  - Use alternative tools instead:
    - write_markdown_file or edit_file for file writing (not heredoc)
    - read_file for reading files (not cat)
    - grep_search for text search (not grep)
    - glob_search for file discovery (not find)"""


SECURITY_BEHAVIOR_TEXT = """\
When a tool call is blocked by security policy:
  - Do NOT retry the exact same operation
  - Adjust your approach: use alternative tools or modified paths
  - If the operation is essential, report the limitation in your output
Tool results may include security error messages with guidance on alternatives."""


# ---------------------------------------------------------------------------
# Config readers (same sources as enforcement)
# ---------------------------------------------------------------------------

def _load_enabled_checks() -> Dict[str, bool]:
    """Load security check toggles from the same config as enforcement.

    Reads from per-agent effective config first, then falls back to
    global config singleton — identical to security.py's _load_enabled_checks.
    """
    try:
        from src.trace import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if isinstance(agent_cfg, dict):
            shell = agent_cfg.get("shell_settings")
            if isinstance(shell, dict):
                checks = shell.get("security_checks")
                if isinstance(checks, dict):
                    return {str(k): bool(v) for k, v in checks.items()}
    except Exception:
        pass

    raw = C.get_nested("shell_settings", "security_checks", default=None)
    if raw is not None and isinstance(raw, dict):
        return {str(k): bool(v) for k, v in raw.items()}
    return {}


def _get_active_check_ids() -> List[str]:
    """Return list of security check IDs that are currently enabled.

    Checks not mentioned in config default to enabled (True).
    """
    overrides = _load_enabled_checks()
    active: List[str] = []
    for check_id in SECURITY_CHECK_DESCRIPTIONS:
        if overrides.get(check_id, True):
            active.append(check_id)
    return active


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_shell_security_section() -> str:
    """Build the Security sandbox section for shell_tool description.

    Reads allowed directories and active security checks from the
    same config sources that enforcement uses, ensuring prompt and
    enforcement are always in sync.

    Returns:
        Structured text suitable for embedding in a tool description.
        Empty string if no security information is available.
    """
    sections: List[str] = []

    # 1. Allowed directories
    try:
        allowed_dirs = get_allowed_directories()
    except Exception:
        allowed_dirs = []

    if allowed_dirs:
        lines = ["Allowed directories (read/write/execute):"]
        for d in allowed_dirs:
            lines.append(f"  - {d}")
        lines.append(
            "Commands targeting paths outside these directories "
            "will be rejected."
        )
        sections.append("\n".join(lines))

    # 2. Active security restrictions
    active_ids = _get_active_check_ids()
    if active_ids:
        lines = ["Active security restrictions:"]
        for check_id in active_ids:
            desc = SECURITY_CHECK_DESCRIPTIONS.get(check_id, check_id)
            lines.append(f"  - {desc}")
        sections.append("\n".join(lines))

    # 3. Denial behavior (always included when there are restrictions)
    if active_ids or allowed_dirs:
        sections.append(DENIAL_BEHAVIOR_TEXT)

    if not sections:
        return ""

    header = "Security sandbox\n"
    return header + "\n\n".join(sections)


def build_security_behavior_section() -> str:
    """Build the security behavior section for the environment prompt.

    Returns static text teaching the AI how to respond to security
    denials — applicable to ALL tools, not just shell_tool.
    """
    return SECURITY_BEHAVIOR_TEXT


# ---------------------------------------------------------------------------
# Shell tool description patching — inject dynamic security policy
# ---------------------------------------------------------------------------

_SHELL_SECURITY_SECTION_MARKER = "Security:"


def _replace_security_section(description: str, new_section: str) -> str:
    """Replace the static Security: section in a tool description with dynamic content.

    Locates the ``Security:`` header and replaces everything up to the next
    major section (``Args:``, ``Returns:``, ``Raises:``, ``Examples:``) or
    end-of-string.

    Args:
        description: The original tool description string.
        new_section: The replacement text (from ``build_shell_security_section``).

    Returns:
        Updated description with the new security section.
    """
    if _SHELL_SECURITY_SECTION_MARKER not in description:
        return description

    # Split at "Security:" and find where the section ends
    marker_pos = description.index(_SHELL_SECURITY_SECTION_MARKER)

    # Find the start of the next section after Security:
    # Use \s* (not \s{4}) because Python strips common docstring indentation
    # when storing __doc__, so section headers may have no leading whitespace.
    next_section_pattern = re.compile(
        r'^\s*(?:Args|Returns|Raises|Examples):',
        re.MULTILINE,
    )
    after_marker = description[marker_pos:]
    # Skip the Security: line itself to search for next section
    skip = after_marker.index('\n') + 1 if '\n' in after_marker else len(after_marker)
    match = next_section_pattern.search(after_marker, skip)

    if match:
        end_pos = marker_pos + match.start()
    else:
        end_pos = len(description)

    replacement = new_section + "\n\n"

    return description[:marker_pos] + replacement + description[end_pos:]


def patch_shell_tool_security(tools: list, log) -> None:
    """Patch shell_tool docstring with dynamic security policy.

    Finds the shell_tool function in the tools list and replaces its
    static Security: section with live policy from ``build_shell_security_section()``.
    The docstring is patched so that the downstream ``ensure_tool_wrapped``
    conversion reads the updated description.

    Args:
        tools: List of tool functions/objects (as returned by get_tools_from_config).
        log: Logger instance.
    """
    for tool_obj in tools:
        name = getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", None)
        if name != "shell_tool":
            continue

        try:
            new_section = build_shell_security_section()
            if not new_section:
                log.info("No security policy to inject into shell_tool description")
                return

            current_doc = getattr(tool_obj, "__doc__", "") or ""
            if not current_doc:
                current_doc = getattr(tool_obj, "description", "") or ""

            if not current_doc:
                return

            patched = _replace_security_section(current_doc, new_section)
            if patched != current_doc:
                # Patch both __doc__ (for ensure_tool_wrapped) and description (for Tool instances)
                if hasattr(tool_obj, "__doc__"):
                    tool_obj.__doc__ = patched
                if hasattr(tool_obj, "description"):
                    tool_obj.description = patched
                log.info("Patched shell_tool description with dynamic security policy")
            else:
                log.info("shell_tool description unchanged (no Security: section found)")
        except Exception as e:
            log.warning(f"Failed to patch shell_tool security section: {e}")
        return  # Only one shell_tool expected
