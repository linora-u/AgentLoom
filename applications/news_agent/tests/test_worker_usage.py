"""A retry must not turn an SDK zero placeholder into confirmed provider usage."""

import json
from pathlib import Path
import shutil

import yaml

from news_agent import baseline
from tests.pi_test.test_codex_application import _fixture


def test_native_http_retries_are_counted_without_inventing_failed_request_usage():
    events = [
        {"kind": "model_request", "provider_request_complete": True,
         "model_turn_id": "turn-one", "attempt": 0},
        {"kind": "model_request", "provider_request_complete": True,
         "model_turn_id": "turn-one", "attempt": 0},
        {"kind": "model_response", "model_turn_id": "turn-one",
         "usage": {"input_tokens": 12, "output_tokens": 3}},
    ]
    measured = baseline._measured_tokens(events)
    assert (measured["provider_requests"], measured["retries"]) == (2, 1)
    assert measured["unreported_provider_requests"] == 1
    assert not measured["token_usage_complete"]


def test_separate_model_turns_are_not_counted_as_http_retries():
    events = [
        {"kind": "model_request", "provider_request_complete": True,
         "model_turn_id": turn, "attempt": 0}
        for turn in ("turn-one", "turn-two")
    ]
    assert baseline._measured_tokens(events)["retries"] == 0


def test_failed_request_remains_unreported_when_a_retry_succeeds(tmp_path, monkeypatch):
    _, wire = _fixture(tmp_path, monkeypatch, search="off", first_status=500,
                       completed=False, answer='{"classifications": []}')
    application = tmp_path / "applications" / "news_agent"
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

    baseline._call_worker(application / "workflows/initial.yaml", "完整新闻分类")
    assert len(wire.read_text().splitlines()) == 2
    manifest = next((application / "data/runtime/runs").rglob("manifest.json"))
    timing = json.loads(manifest.read_text())["performance"]
    assert (timing["provider_requests"], timing["retries"]) == (2, 1)
    assert (timing["input_tokens"], timing["output_tokens"]) == (12, 3)
    assert timing["usage_responses"] == 1
    assert timing["unreported_provider_requests"] == 1
    assert not timing["token_usage_complete"]
