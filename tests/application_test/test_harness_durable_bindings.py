"""Real DBOS threads, native Host processes and Pi SDK; local HTTP model only."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import yaml
from agentloom.app import Application
from agentloom.config.config import bind_harness_model_config, current_harness_model_config
from agentloom.config.private_bindings import (
    bind_harness_credentials,
    capture_harness_bindings,
    forward_harness_bindings,
    resolve_bound_credentials,
)
from agentloom.execution.concurrency import WorkItem
from agentloom.runtimes.pi.credentials import bind_pi_credentials, current_pi_credentials

from tests.pi_test.test_application import model_service, project


def test_durable_executor_preserves_private_bindings_across_dbos_and_sdk(tmp_path):
    with model_service() as (url, requests):
        definition = project(tmp_path, url)
        application = definition.parents[1]
        models = yaml.safe_load((tmp_path / "config/model.yaml").read_text())
        # The live source cannot supply working credentials or a model catalog.
        (tmp_path / "config/model.yaml").write_text("invalid: [")
        namespace = tmp_path / "durable"
        with bind_harness_credentials({"fixture#/private": "dummy-private-capability"}), bind_harness_model_config(models), bind_pi_credentials({"fixture": {"type": "oauth", "access": "dummy-pi-capability"}}):
            with Application.open(application, project_root=tmp_path, file_logging=False) as app, app.durable(root=namespace) as execution:
                handle = execution.worker("workflows/root.yaml").submit(WorkItem("private", {}))
                with forward_harness_bindings() as (overlay, descriptors), (tmp_path / "consumer.log").open("w") as log:
                    process = subprocess.Popen([sys.executable, "-m", "test_support.durable_harness", str(tmp_path), str(application), str(namespace)],
                        env={**os.environ, **overlay}, pass_fds=descriptors, stdout=log, stderr=log)
                try:
                    assert process.wait(timeout=45) == 0, (tmp_path / "consumer.log").read_text()
                    assert execution.status(handle.workflow_id).status == "SUCCESS", (tmp_path / "consumer.log").read_text()
                    assert handle.get_result()["output"] == "Pi answer"
                    assert execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"]
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
        assert len(requests) == 1
        assert {key.lower(): value for key, value in requests[0][2].items()}["authorization"] == "Bearer fixture-secret"
        for path in namespace.rglob("*.json"):
            payload = path.read_text()
            assert "dummy-private-capability" not in payload
            assert "dummy-pi-capability" not in payload
        # Leaving the caller's scope does not retain credentials in this thread.
        assert current_pi_credentials() is None
        assert current_harness_model_config() is None
        assert resolve_bound_credentials("harness-credential-ref:fixture#/private") == "harness-credential-ref:fixture#/private"


def test_executor_capabilities_are_separate_and_clear_after_concurrent_callback_failure():
    scopes = {}
    for namespace in ("first", "second"):
        catalog = {"model": {"default_model_type": "test", "test": {"model": "dummy", "adapter": "openai_chat", "api_key": namespace}}}
        with bind_harness_credentials({"namespace": namespace}), bind_harness_model_config(catalog), bind_pi_credentials({"fixture": {"access": namespace}}):
            scopes[namespace] = capture_harness_bindings()
    rendezvous = Barrier(2)

    def callback(namespace):
        assert current_harness_model_config() is None
        with pytest.raises(LookupError), scopes[namespace]():
            rendezvous.wait(timeout=5)
            assert resolve_bound_credentials("harness-credential-ref:namespace") == namespace
            assert current_harness_model_config()["models"]["test"]["api_key"] == namespace
            assert current_pi_credentials()["fixture"]["access"] == namespace
            raise LookupError("Callback failed after capability use")
        assert current_harness_model_config() is None
        assert current_pi_credentials() is None
        assert resolve_bound_credentials("harness-credential-ref:namespace") == "harness-credential-ref:namespace"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(callback, namespace) for namespace in scopes]
        for future in futures:
            future.result(timeout=10)
