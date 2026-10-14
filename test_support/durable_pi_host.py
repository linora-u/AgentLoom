"""Genuine Pi/MCP Application fixture inside a durable owned invocation."""
import signal
import sys
from pathlib import Path
from threading import Event

from agentloom.app import Application
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.durable import HostSpec

from tests.acceptance.worker_batch_support import FixtureState, capture_bridges, configure


class PiFixtureHost:
    def __init__(self, options):
        self.root = Path(options["project"])
        self.mode = options["mode"]

    def reconcile(self, task):
        return None

    def revoke(self, task, invocation_id):
        pass

    def invoke(self, task, invocation_id):
        state = FixtureState(self.root, mode=self.mode, count=1)
        application = self.root / "applications" / "batch_acceptance"
        with configure(state), capture_bridges(self.root), bind_config(load_project_config(self.root)):
            with Application.open(application, project_root=self.root, file_logging=False) as app:
                return app.worker(task.worker).invoke(task.item).to_dict()


def fixture_host(options):
    return PiFixtureHost(options)


def main():
    project, namespace, mode = sys.argv[1:4]
    project, namespace = Path(project), Path(namespace)
    application = project / "applications" / "batch_acceptance"
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    host = HostSpec("test_support.durable_pi_host:fixture_host", {"project": str(project), "mode": mode})
    with Application.open(application, project_root=project, file_logging=False) as app, app.durable(root=namespace) as execution:
        execution.executor(["workflows/worker.yaml"], host=host).run(stop_token=stop)


if __name__ == "__main__":
    main()
