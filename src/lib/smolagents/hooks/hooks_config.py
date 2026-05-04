"""Hook configuration management and snapshot support.

Loads ``HookMatcher`` lists from YAML/dict configuration and provides an
immutable snapshot mechanism to prevent runtime config changes from
affecting in-flight hook execution.

Aligned with upstream ``hooksConfigManager.ts`` / ``hooksConfigSnapshot.ts``.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from src.lib.logging import get_logger

from .hook_helpers import parse_hook_command
from .types import HookCommand, HookEvent, HookMatcher, HooksSettings

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_hooks_from_dict(raw: Dict[str, Any]) -> HooksSettings:
    """Parse a raw dict (from YAML ``hooks:`` section) into typed settings.

    Expected structure::

        {
          "PreToolUse": [
            {
              "matcher": "Write",
              "hooks": [
                {"type": "command", "command": "bash check.sh", "timeout": 5}
              ]
            }
          ],
          ...
        }

    Unrecognised event names or hook types are skipped with a warning.
    """
    settings: HooksSettings = {}

    if not isinstance(raw, dict):
        return settings

    # Build a lookup from event string value -> canonical event name
    event_values = {e.value: e.value for e in HookEvent}

    for event_key, matchers_raw in raw.items():
        # Resolve event name
        canonical = event_values.get(event_key)
        if canonical is None:
            logger.warning("Unknown hook event '%s' in config, skipping", event_key)
            continue

        if not isinstance(matchers_raw, list):
            logger.warning(
                "Hook event '%s' config must be a list of matchers, got %s",
                event_key, type(matchers_raw).__name__,
            )
            continue

        parsed_matchers: List[HookMatcher] = []
        for matcher_raw in matchers_raw:
            if not isinstance(matcher_raw, dict):
                continue

            matcher_str = matcher_raw.get("matcher")
            hooks_raw = matcher_raw.get("hooks", [])
            if not isinstance(hooks_raw, list):
                continue

            parsed_hooks: List[HookCommand] = []
            for hook_raw in hooks_raw:
                if not isinstance(hook_raw, dict):
                    continue
                cmd = parse_hook_command(hook_raw)
                if cmd is not None:
                    parsed_hooks.append(cmd)

            if parsed_hooks:
                parsed_matchers.append(HookMatcher(
                    matcher=matcher_str,
                    hooks=parsed_hooks,
                ))

        if parsed_matchers:
            settings[canonical] = parsed_matchers

    return settings


# ---------------------------------------------------------------------------
# Config snapshot (immutable copy for execution safety)
# ---------------------------------------------------------------------------

class HooksConfigSnapshot:
    """Immutable snapshot of hooks configuration.

    Created at the start of a hook execution batch to ensure that
    configuration changes during execution do not affect the current
    batch.  Aligned with upstream ``captureHooksConfigSnapshot()``.
    """

    def __init__(self, settings: HooksSettings) -> None:
        # Deep-copy to ensure immutability
        self._settings: HooksSettings = copy.deepcopy(settings)

    def get_matchers(self, event: str) -> List[HookMatcher]:
        """Return matchers for the given event name (canonical string)."""
        return list(self._settings.get(event, []))

    def get_all_events(self) -> List[str]:
        """Return all event names that have at least one matcher."""
        return list(self._settings.keys())

    @property
    def settings(self) -> HooksSettings:
        """Return a read-only reference to the settings dict."""
        return self._settings


# ---------------------------------------------------------------------------
# Global config holder
# ---------------------------------------------------------------------------

class HooksConfigManager:
    """Manages the active hooks configuration with snapshot support.

    Aligned with upstream ``hooksConfigManager`` pattern: call
    ``update()`` when settings change, call ``snapshot()`` before
    executing a hook batch.
    """

    def __init__(self) -> None:
        self._settings: HooksSettings = {}
        self._enabled: bool = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def update(self, raw: Dict[str, Any]) -> None:
        """Update the active configuration from a raw dict."""
        self._settings = load_hooks_from_dict(raw)
        logger.debug(
            "Hooks config updated: %d events configured",
            len(self._settings),
        )

    def snapshot(self) -> HooksConfigSnapshot:
        """Capture an immutable snapshot of the current configuration."""
        return HooksConfigSnapshot(self._settings)

    def get_matchers(self, event: str) -> List[HookMatcher]:
        """Get matchers for an event (live, not snapshotted)."""
        return list(self._settings.get(event, []))

    def clear(self) -> None:
        """Clear all configuration."""
        self._settings = {}
