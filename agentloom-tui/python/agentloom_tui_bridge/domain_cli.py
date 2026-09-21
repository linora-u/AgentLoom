"""Private TUI adapter entry point for domain actions."""

from agentloom.application.studio.domain_cli import _dispatch, main

__all__ = ["_dispatch", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
