"""Canonical batch evidence at the Application lifecycle storage seam."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from agentloom.app.lifecycle import ApplicationRunLifecycle
from agentloom.config.redaction import bind_redaction_policy
from agentloom.execution import RuntimeHome


def test_application_lifecycle_keeps_separate_batch_records_and_root_references(tmp_path):
    context = RuntimeHome(tmp_path / "runtime").new_context(application_id="batch-storage")
    context.prepare_run()
    context.write_manifest()
    lifecycle = ApplicationRunLifecycle()
    start = Barrier(2)

    def persist(batch_id, concurrency):
        with bind_redaction_policy({"redaction": {"enabled": True}}):
            context.write_batch_record(batch_id, {
                "concurrency": concurrency,
                "source": "call",
                "credential_probe": {"api_key": "fixture-credential"},
            })
        start.wait(timeout=5)
        lifecycle.register_batch_record(context, batch_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(persist, "batch-first", 4)
        second = pool.submit(persist, "batch-second", 20)
        first.result(timeout=10)
        second.result(timeout=10)

    manifest = json.loads(context.read_run_file(context.manifest_path))
    assert sorted(manifest["worker_batches"], key=lambda ref: ref["batch_id"]) == [
        {"batch_id": "batch-first", "record": "batches/batch-first.json"},
        {"batch_id": "batch-second", "record": "batches/batch-second.json"},
    ]
    first_record = context.read_batch_record("batch-first")
    second_record = context.read_batch_record("batch-second")
    assert first_record["concurrency"] == 4 and second_record["concurrency"] == 20
    assert first_record["run_id"] == second_record["run_id"] == context.run_id
    assert "fixture-credential" not in context.read_run_file(context.batch_record_path("batch-first"))
