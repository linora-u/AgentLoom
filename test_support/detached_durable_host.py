"""Default durable consumer for genuine Pi/model-HTTP fixture lifecycle tests."""
import signal
import sys
from pathlib import Path
from threading import Event

from agentloom.app import Application

root = Path(sys.argv[1])
stop = Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
signal.signal(signal.SIGINT, lambda *_: stop.set())
with Application.open(root / "applications/pi", project_root=root, file_logging=False) as application:
    with application.durable(root=root / "durable") as execution:
        execution.executor(["workflows/root.yaml"]).run(stop_token=stop)
