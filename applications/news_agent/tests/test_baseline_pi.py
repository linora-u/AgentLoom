"""Verify news_agent Pi Codex subscription requests without live network."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from news_agent import baseline

from tests.pi_test.test_codex_application import _fixture


def test_service_tier_changes_news_model_signature(tmp_path: Path, monkeypatch) -> None:
    source_root = baseline.ROOT
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("model.yaml", "system.yaml"):
        shutil.copyfile(source_root / "config" / name, config_dir / name)
    for name in ("baseline.py", "settings.py", "run.py", "report.py", "skill_reference.py"):
        shutil.copyfile(source_root / name, tmp_path / name)
    monkeypatch.setattr(baseline, "ROOT", tmp_path)
    original_signature = baseline._model_signature()
    path = config_dir / "model.yaml"
    profiles = yaml.safe_load(path.read_text())
    for profile in profiles["model"].values():
        profile["service_tier"] = "default"
    path.write_text(yaml.safe_dump(profiles))
    assert baseline._model_signature() != original_signature


@pytest.mark.parametrize(("workflow", "prompt", "answer", "expected"), [
    ("initial", "初筛.md", '{"classifications": []}', {"classifications": []}),
    ("refined", "精筛.md", '{"reviews": []}', { "reviews": []}),
])
def test_news_worker_uses_codex_fast_with_only_its_allowed_tools(
    tmp_path: Path, monkeypatch, workflow: str, prompt: str, answer: str, expected: dict,
) -> None:
    _, wire = _fixture(tmp_path, monkeypatch, search="off", completed=workflow != "initial",
                       answer=answer, response_tier="default")
    application = tmp_path / "applications" / "news_agent"
    for relative in ("config/model.yaml", "config/system.yaml",
                     f"workflows/{workflow}.yaml", f"agent/{prompt}"):
        destination = application / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline.ROOT / relative, destination)
    monkeypatch.setattr(baseline, "ROOT", application)

    result = baseline._call_worker(application / "workflows" / f"{workflow}.yaml",
                                   '证据截止=2025-08-17T23:59:59+08:00；mode=historical_2025；历史日期=2025-08-17。记录ID: N1；正文: 历史新闻。')
    assert result == expected
    requests = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    assert len(requests) == 1
    payload = requests[0]["body"]
    assert payload["model"] == "gpt-6-luna"
    assert payload["max_output_tokens"] == 65536
    assert requests[0]["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert payload["service_tier"] == "priority"
    # AgentLoom supplies its task-scoped context reader whenever tools are present.
    allowed = set() if workflow != "refined" else {
        "skill", "read_news_reference", "loom_retrieve_context",
    }
    assert {tool["name"] for tool in payload.get("tools", []) if tool.get("type") != "web_search"} == allowed
    searches = [tool for tool in payload.get("tools", []) if tool.get("type") == "web_search"]
    assert bool(searches) == (workflow != "initial")
    assert "future_prices" not in json.dumps(payload, ensure_ascii=False)
    manifest = next((application / "data/runtime/runs").rglob("manifest.json"))
    timing = json.loads(manifest.read_text())["performance"]
    assert (timing["provider_requests"], timing["retries"], timing["input_tokens"],
            timing["output_tokens"]) == (1, 0, 12, 3)
    assert timing["trace_dir"] and timing["status"] == "completed"


def test_provider_retry_is_measured_and_raw_responses_remain_available(tmp_path, monkeypatch):
    _, wire = _fixture(tmp_path, monkeypatch, search="off", first_status=500,
                       completed=False, answer='{"classifications": []}',
                       response_tier="default")
    application = tmp_path / "applications/news_agent"
    for relative in ("config/model.yaml", "config/system.yaml",
                     "workflows/initial.yaml", "agent/初筛.md"):
        destination = application / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline.ROOT / relative, destination)
    path = application / "config/model.yaml"
    config = yaml.safe_load(path.read_text())
    for profile in config["model"].values():
        profile.update(retry_delay=0, max_retry_delay=0)
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(baseline, "ROOT", application)
    assert baseline._call_worker(application / "workflows/initial.yaml", '证据截止=2025-08-17T23:59:59+08:00；全部新闻为行情复述') == {"classifications": []}
    requests = [json.loads(line)["body"] for line in wire.read_text().splitlines()]
    assert len(requests) == 2
    assert all(request["service_tier"] == "priority" for request in requests)
    manifest = next((application / "data/runtime/runs").rglob("manifest.json"))
    timing = json.loads(manifest.read_text())["performance"]
    assert (timing["provider_requests"], timing["retries"]) == (2, 1)
    response_events = [json.loads(path.read_text()) for path in Path(timing["trace_dir"]).rglob("*.json")
                       if path.parent.name == timing["run_id"]]
    responses = [event for event in response_events if event.get("kind") == "model_response"]
    responses.sort(key=lambda event: event["sequence"])
    # The SDK performs both HTTP requests within one stream and reports its final
    # response once. The failed request's usage remains explicitly unreported.
    assert [event["status"] for event in responses] == ["completed"]
    assert all(event["response_ref"] for event in responses)
    wire_events = [event for event in response_events if event.get("kind") == "model_request"
                   and event.get("provider_request_complete")]
    assert len(wire_events) == 2
    assert len({event["model_turn_id"] for event in wire_events + responses}) == 1
    assert timing["unreported_provider_requests"] == 1
    assert not timing["token_usage_complete"]
