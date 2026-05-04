"""LSP configuration — loaded from system.yaml ``lsp_servers`` section."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)


@dataclass
class LSPServerConfig:
    """Configuration for a single language server."""

    language: str
    enabled: bool = True
    max_restarts: int = 3


@dataclass
class LSPConfig:
    """Global LSP configuration from ``system.yaml``."""

    enabled: bool = True
    max_restarts: int = 3
    servers: List[LSPServerConfig] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, yaml_dict: Optional[Dict]) -> LSPConfig:
        """Build from the ``lsp_servers`` section of system.yaml.

        Example YAML::

            lsp_servers:
              enabled: true
              max_restarts: 3        # global default
              servers:
                - python
                - go
                - typescript
        """
        if not yaml_dict:
            return cls(enabled=True, servers=[LSPServerConfig(language="python")])

        enabled = yaml_dict.get("enabled", True)
        global_max_restarts = yaml_dict.get("max_restarts", 3)
        raw_servers = yaml_dict.get("servers", [])

        servers: List[LSPServerConfig] = []
        for entry in raw_servers:
            if isinstance(entry, str):
                servers.append(LSPServerConfig(language=entry))
            elif isinstance(entry, dict):
                servers.append(
                    LSPServerConfig(
                        language=entry.get("language", ""),
                        enabled=entry.get("enabled", True),
                        max_restarts=entry.get("max_restarts", global_max_restarts),
                    )
                )

        if not servers:
            servers = [LSPServerConfig(language="python")]

        return cls(enabled=enabled, max_restarts=global_max_restarts, servers=servers)
