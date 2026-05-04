"""MCP configuration parsing and validation.

Handles loading ``.mcp.json`` files (Claude Code compatible format),
parsing the ``mcp_servers`` YAML value (string / list / dict), path
resolution from ``agent_root``, and multi-level config merging.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_VALID_TRANSPORT_TYPES = {"stdio", "sse", "http", "streamable-http"}


@dataclass(frozen=True)
class McpServerConfig:
    """Parsed configuration for one MCP server."""

    name: str                                         # server name (from JSON key)
    type: str                                         # "stdio" | "sse" | "streamable-http"
    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http/sse fields
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class McpSettings:
    """Parsed ``mcp_servers`` YAML value with global options."""

    configs: list[McpServerConfig] = field(default_factory=list)
    timeout: int = 30
    tool_timeout: int = 60
    tool_name_prefix: bool = True


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_mcp_json_path(raw_path: str, agent_root: Path) -> Path:
    """Resolve MCP JSON path relative to *agent_root* (same rule as ``prompt.path``).

    * Relative path -> ``agent_root / relative_path``
    * Absolute path -> used as-is
    * ``~`` expansion supported
    """
    expanded = Path(raw_path).expanduser()
    if expanded.is_absolute():
        return expanded
    return (agent_root / expanded).resolve()


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------


def load_mcp_json(path: Path) -> dict:
    """Load and validate a ``.mcp.json`` file.

    Returns the parsed dict.  Raises :class:`FileNotFoundError` if the file
    does not exist, or :class:`ValueError` on invalid JSON / missing
    ``mcpServers`` key.
    """
    if not path.exists():
        raise FileNotFoundError(f"MCP config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(data).__name__}")

    if "mcpServers" not in data:
        raise ValueError(f"Missing 'mcpServers' key in {path}")

    return data


# ---------------------------------------------------------------------------
# Server config parsing
# ---------------------------------------------------------------------------

_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


def sanitize_server_name(name: str) -> str:
    """Sanitize a server name for use in tool name prefixes.

    Replaces any character that is not ``[a-zA-Z0-9]`` with ``_``.
    """
    return _NAME_SANITIZE_RE.sub("_", name)


def _parse_one_server(name: str, raw: dict[str, Any]) -> McpServerConfig:
    """Parse a single server entry from ``.mcp.json``.

    Validates required fields per transport type and normalises
    ``"http"`` -> ``"streamable-http"``.
    """
    transport = raw.get("type", "stdio")
    if isinstance(transport, str):
        transport = transport.strip().lower()

    # Normalise "http" to "streamable-http" (convenience alias).
    if transport == "http":
        transport = "streamable-http"

    if transport not in _VALID_TRANSPORT_TYPES:
        raise ValueError(
            f"Server '{name}': unsupported transport type '{transport}'. "
            f"Supported: {', '.join(sorted(_VALID_TRANSPORT_TYPES))}"
        )

    if transport == "stdio":
        command = raw.get("command", "")
        if not command:
            raise ValueError(f"Server '{name}': 'command' is required for stdio transport")
        return McpServerConfig(
            name=name,
            type=transport,
            command=command,
            args=list(raw.get("args", [])),
            env=dict(raw.get("env", {})),
        )

    # sse / streamable-http
    url = raw.get("url", "")
    if not url:
        raise ValueError(f"Server '{name}': 'url' is required for {transport} transport")
    return McpServerConfig(
        name=name,
        type=transport,
        url=url,
        headers=dict(raw.get("headers", {})),
    )


def parse_mcp_servers_from_json(data: dict) -> list[McpServerConfig]:
    """Parse ``mcpServers`` dict from JSON into a list of :class:`McpServerConfig`.

    Skips invalid entries with a warning instead of raising.
    """
    servers_raw = data.get("mcpServers", {})
    if not isinstance(servers_raw, dict):
        logger.warning("[MCP] 'mcpServers' value is not a dict, skipping")
        return []

    configs: list[McpServerConfig] = []
    for name, entry in servers_raw.items():
        if not isinstance(entry, dict):
            logger.warning("[MCP] Server '%s': expected dict, got %s — skipping", name, type(entry).__name__)
            continue
        try:
            configs.append(_parse_one_server(name, entry))
        except ValueError as exc:
            logger.warning("[MCP] %s — skipping", exc)
    return configs


# ---------------------------------------------------------------------------
# YAML value parsing  (mcp_servers field)
# ---------------------------------------------------------------------------


def _load_configs_from_paths(
    paths: list[str],
    agent_root: Path,
) -> list[McpServerConfig]:
    """Resolve, load, and parse a list of JSON paths.  Failures are logged as
    warnings; valid configs are returned.
    """
    configs: list[McpServerConfig] = []
    for raw_path in paths:
        resolved = resolve_mcp_json_path(raw_path, agent_root)
        try:
            data = load_mcp_json(resolved)
            parsed = parse_mcp_servers_from_json(data)
            configs.extend(parsed)
            logger.info("[MCP] Loaded config from '%s': %d servers", resolved, len(parsed))
        except FileNotFoundError:
            logger.warning("[MCP] Config file not found: %s", resolved)
        except ValueError as exc:
            logger.warning("[MCP] %s", exc)
    return configs


def parse_mcp_yaml_value(
    raw_value: Any,
    agent_root: Path,
) -> Optional[McpSettings]:
    """Parse the ``mcp_servers`` YAML value into :class:`McpSettings`.

    Accepted YAML formats::

        # Option 1: single file path (string)
        mcp_servers: "config/.mcp.json"

        # Option 2: multiple file paths (list of strings)
        mcp_servers:
          - "config/.mcp.json"
          - "config/extra-mcp.json"

        # Option 3: dict with options
        mcp_servers:
          path: "config/.mcp.json"            # or paths: [...]
          timeout: 30
          tool_timeout: 60
          tool_name_prefix: true

    Returns ``None`` when *raw_value* is ``None`` / empty / invalid.
    """
    if raw_value is None:
        return None

    # Option 1: single string path
    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if not raw_value:
            return None
        configs = _load_configs_from_paths([raw_value], agent_root)
        return McpSettings(configs=configs)

    # Option 2: list of string paths
    if isinstance(raw_value, list):
        paths = [str(p).strip() for p in raw_value if isinstance(p, str) and str(p).strip()]
        if not paths:
            return None
        configs = _load_configs_from_paths(paths, agent_root)
        return McpSettings(configs=configs)

    # Option 3: dict with path/paths + options
    if isinstance(raw_value, dict):
        paths: list[str] = []
        if "path" in raw_value:
            p = raw_value["path"]
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
        if "paths" in raw_value:
            ps = raw_value["paths"]
            if isinstance(ps, list):
                paths.extend(str(x).strip() for x in ps if isinstance(x, str) and str(x).strip())

        if not paths:
            logger.warning("[MCP] mcp_servers dict has no 'path' or 'paths' — skipping")
            return None

        configs = _load_configs_from_paths(paths, agent_root)
        return McpSettings(
            configs=configs,
            timeout=int(raw_value.get("timeout", 30)),
            tool_timeout=int(raw_value.get("tool_timeout", 60)),
            tool_name_prefix=bool(raw_value.get("tool_name_prefix", True)),
        )

    logger.warning("[MCP] Unexpected mcp_servers type: %s — skipping", type(raw_value).__name__)
    return None


# ---------------------------------------------------------------------------
# Multi-level config merging
# ---------------------------------------------------------------------------


def merge_mcp_configs(
    global_settings: Optional[McpSettings],
    agent_settings: Optional[McpSettings],
) -> Optional[McpSettings]:
    """Merge global and agent-level MCP settings.

    Merge rules:
    1. Load global config first -> global server list.
    2. Load agent YAML config -> agent-level server list.
    3. Same-name servers: agent-level overrides global.
    4. New server names at agent level are added.
    5. Options (timeout etc.) from agent_settings take precedence when present.

    Returns ``None`` when both inputs are ``None``.
    """
    if global_settings is None and agent_settings is None:
        return None
    if global_settings is None:
        return agent_settings
    if agent_settings is None:
        return global_settings

    # Merge configs: agent overrides global by server name.
    merged_map: dict[str, McpServerConfig] = {}
    for cfg in global_settings.configs:
        merged_map[cfg.name] = cfg
    for cfg in agent_settings.configs:
        merged_map[cfg.name] = cfg  # override or add

    # Agent-level options take precedence.
    return McpSettings(
        configs=list(merged_map.values()),
        timeout=agent_settings.timeout,
        tool_timeout=agent_settings.tool_timeout,
        tool_name_prefix=agent_settings.tool_name_prefix,
    )


# ---------------------------------------------------------------------------
# Conversion to smolagents MCPClient parameters
# ---------------------------------------------------------------------------


def to_mcp_client_params(config: McpServerConfig) -> Any:
    """Convert :class:`McpServerConfig` to parameters accepted by
    ``smolagents.MCPClient``.

    * ``stdio`` -> ``mcp.StdioServerParameters``
    * ``sse`` / ``streamable-http`` -> ``dict`` with ``url`` + ``transport``
    """
    if config.type == "stdio":
        try:
            from mcp import StdioServerParameters
        except ImportError:  # pragma: no cover
            raise ImportError(
                "mcp package is required for stdio transport. "
                "Install with: uv pip install 'smolagents[mcp]'"
            )
        params = StdioServerParameters(
            command=config.command,
            args=config.args,
        )
        if config.env:
            # StdioServerParameters expects env as dict[str, str] | None
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env,
            )
        return params

    # sse / streamable-http
    params: dict[str, Any] = {
        "url": config.url,
        "transport": config.type,
    }
    if config.headers:
        params["headers"] = config.headers
    return params
