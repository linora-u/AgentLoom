"""Actual DBOS/SDK consumer with private, dummy-only capability assertions."""
from __future__ import annotations

import sys
from pathlib import Path

from agentloom.app import Application
from agentloom.config.config import current_harness_model_config
from agentloom.config.private_bindings import consume_harness_bindings, resolve_bound_credentials
from agentloom.execution.durable import HostSpec
from agentloom.execution.durable.host import ApplicationHost
from agentloom.runtimes.pi.credentials import current_pi_credentials


def check_bindings():
    assert resolve_bound_credentials("harness-credential-ref:fixture#/private") == "dummy-private-capability"
    assert current_harness_model_config()["models"]["test"]["api_key"] == "fixture-secret"
    assert current_pi_credentials() == {"fixture": {"type": "oauth", "access": "dummy-pi-capability"}}


class PrivateHost(ApplicationHost):
    def authorize(self, task, invocation_id, assert_current):
        check_bindings()  # Runs in DBOS's callback/thread context, before exec.
        assert_current()

    def invoke(self, task, invocation_id):
        check_bindings()  # Runs in the actual, separately owned Host process.
        return super().invoke(task, invocation_id)


def private_host(options):
    return PrivateHost(options)


def main():
    project, application, namespace = map(Path, sys.argv[1:4])
    with consume_harness_bindings():
        check_bindings()
        with Application.open(application, project_root=project, file_logging=False) as app, app.durable(root=namespace) as execution:
            host = HostSpec("test_support.durable_harness:private_host", {"application": str(application), "project_root": str(project)})
            execution.executor(["workflows/root.yaml"], host=host).run(idle_timeout=0.3)


if __name__ == "__main__":
    main()
