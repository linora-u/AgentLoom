"""Official News CLI with a no-model registry and a controlled SQL boundary."""
import sys
from pathlib import Path

import pytest

from test_support.worker_batch import batch_project


def main():
    project, application, campaign, prices, candidates, config, fault = map(Path, sys.argv[1:])
    patch = pytest.MonkeyPatch()
    fixture = batch_project.__wrapped__(project, patch)
    next(fixture)
    from news_agent.runtime import worker
    from news_agent.service import main as news_main

    patch.setattr(worker, "APP_ROOT", application)
    import sqlite3

    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import OperationalError

    def fail_terminal(connection, cursor, statement, parameters, context, executemany):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if (fault.exists() and statement.lstrip().upper().startswith("UPDATE ")
                and ("SUCCESS" in values or "ERROR" in values)):
            error = sqlite3.OperationalError("injected News terminal persistence failure")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            error.sqlite_errorname = "SQLITE_FULL"
            raise OperationalError("UPDATE fixture", {}, error) from error

    event.listen(Engine, "before_cursor_execute", fail_terminal)
    try:
        sys.argv = ["news_agent.service", "--root", str(campaign), "--prices", str(prices),
                    "--candidates", str(candidates), "--config", str(config), "executor"]
        news_main()
    finally:
        event.remove(Engine, "before_cursor_execute", fail_terminal)
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()


if __name__ == "__main__":
    main()
