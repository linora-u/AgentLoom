"""Curated records stay reviewed and partitioned; business memory excludes them."""

from agentloom.app.harnessing.library import HarnessLibrary
from agentloom.app.harnessing.snapshot import snapshot_application
from agentloom.app.harnessing.models import ApplicationRequest, PreparedInvocation, ExecutionReceipt


def test_reviewed_library_has_archives_and_cannot_leak_into_ordinary_memory(tmp_path):
    root = tmp_path / "project"
    app = root / "applications/sample"
    (root / "config").mkdir(parents=True)
    (root / "config/system.yaml").write_text("{}")
    (app / "prompts").mkdir(parents=True)
    (app / "prompts/main.md").write_text("FULL ARCHIVED HARNESS REFERENCE")
    bundle = snapshot_application(root, app, tmp_path / "bundles")
    config = {"application_id": "sample", "self_learning": {"enabled": True}}
    library = HarnessLibrary("sample", db_path=tmp_path / "memory.db", agent_config=config)
    for key, mode, partition, available in [
        ("CONTRASTIVE", "contrastive", "", "2026-10-18T00:00:00+00:00"),
        ("DEVA", "development_score", "train-a", "2026-10-18T00:00:00+00:00"),
        ("DEVB", "development_score", "train-b", "2026-10-18T00:00:00+00:00"),
        ("FUTURE", "contrastive", "", "2026-10-22T00:00:00+00:00"),
    ]:
        value = library.memory.add_typed(
            "app",
            kind="experience",
            memory_key="harness:" + key,
            payload={
                "trigger": key,
                "symptom": "Missing observed evidence",
                "action": "Read source",
                "verification": "Observed original evidence",
            },
            scope_id="sample",
            agent_config=config,
            provenance=(
                {
                    "kind": "harness_experiment",
                    "feedback": mode,
                    "partition": partition,
                    "available_at": available,
                    "candidate_root": str(bundle.root),
                    "candidate_id": bundle.candidate_id,
                },
            ),
        )
        assert value["ok"]
    assert library.memory.add_typed(
        "app",
        kind="fact",
        memory_key="ordinary:fact",
        payload={"text": "NORMAL BUSINESS FACT"},
        scope_id="sample",
        agent_config=config,
    )["ok"]
    contrastive = library.view(feedback="contrastive", cutoff="2026-10-20T00:00:00+00:00")
    assert len(contrastive) == 1 and contrastive[0]["payload"]["trigger"] == "CONTRASTIVE"
    assert (
        contrastive[0]["harnesses"][0]["files"]["applications/sample/prompts/main.md"]
        == "FULL ARCHIVED HARNESS REFERENCE"
    )
    development = library.view(feedback="development_score", partition="train-a")
    assert len(development) == 1 and development[0]["payload"]["trigger"] == "DEVA"
    snapshot = library.memory.snapshot_for_prompt(agent_config=config)
    assert "NORMAL BUSINESS FACT" in snapshot
    assert not any(key in snapshot for key in ("CONTRASTIVE", "DEVA", "DEVB", "FUTURE"))
    listed = library.memory.handle_tool_action("list", scope="app", scope_id="sample", agent_config=config)
    assert [item["memory_key"] for item in listed["items"]] == ["ordinary:fact"]
    request = ApplicationRequest("sample", root, app, "app:Driver", task="original facts")
    invocation = PreparedInvocation(request, "Original task", {}, {})
    receipt = ExecutionReceipt("final1", bundle.candidate_id, "completed", accepted=True, cleanup_complete=True)
    proposed = library.submit(
        [
            {
                "trigger": "new task",
                "symptom": "missing source",
                "action": "inspect source",
                "verification": "source read succeeded",
                "evidence_refs": ["final1"],
            }
        ],
        selected=receipt,
        bundle=bundle,
        invocation=invocation,
        experiment_id="experiment",
        reflection_run_id="root-reflection",
        feedback="contrastive",
        available_at="2026-10-19T00:00:00+00:00",
    )
    assert proposed["submitted"] == 1
    assert proposed["candidates"][0]["ok"]
    assert proposed["candidates"][0]["state"] != "active_confirmed"
    assert all(item["payload"]["trigger"] != "new task" for item in library.view(feedback="contrastive"))
