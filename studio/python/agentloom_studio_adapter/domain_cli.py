"""Versioned JSON entry point for Application domain actions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agentloom.application.studio.domain_actions import execute_domain_action
from agentloom.application.studio.errors import StudioServiceError

CONTRACT_VERSION = 1
_MAX_PARAMS_BYTES = 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloom-studio-domain")
    parser.add_argument("--project", required=True)
    parser.add_argument("action")
    parser.add_argument("params", nargs="?", default="{}")
    args = parser.parse_args(argv)

    try:
        params = _params(args.params)
        result = execute_domain_action(
            Path(args.project),
            args.action,
            params,
        )
    except StudioServiceError as error:
        _write(
            {
                "contract_version": CONTRACT_VERSION,
                "ok": False,
                "error": {"code": error.code, "message": str(error)},
            }
        )
        return 2
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        _write(
            {
                "contract_version": CONTRACT_VERSION,
                "ok": False,
                "error": {"code": "invalid_params", "message": str(error)},
            }
        )
        return 2

    _write(
        {
            "contract_version": CONTRACT_VERSION,
            "ok": True,
            "result": result,
        }
    )
    return 0


def _params(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _MAX_PARAMS_BYTES:
        raise ValueError("params exceeded the safe size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("params must be a JSON object")
    return value


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
