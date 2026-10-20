"""All ordinary role contracts cross the published Pi SDK and actual HTTP wire."""
from __future__ import annotations

import json

import pytest
import yaml
from agentloom.app import Application
from agentloom.app.harnessing.roles import _RESOURCE_ROOT, validate_role_output
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency import WorkItem

from tests.application_test.mixed_runtime_support import assert_closed_output_schema
from tests.pi_test.test_application import change_model, model_service, project


@pytest.mark.parametrize("responses", [False, True])
def test_every_role_has_a_closed_actual_provider_schema_and_validated_output(tmp_path, responses):
    outputs = {
        "general": {"edits": [{"path": "applications/pi/prompts/new.md", "content": "NEW"}], "rationale": "Add a supported instruction."},
        "domain": {"edits": [], "rationale": "No supported change."},
        "compose": {"edits": [{"path": "applications/pi/prompts/old.md", "content": None}], "rationale": "Remove an obsolete instruction."},
        "select": {"chosen_id": "C1", "reason": "Satisfies the supplied acceptance."},
        "reflect": {"experiences": [{"trigger": "Evidence exists", "symptom": "Redundant work", "action": "Remove duplication", "verification": "Observed in the supplied probe", "evidence_refs": ["probe1"]}]},
    }
    schemas = {}

    def provider_check(number, request):
        contract = request["text"]["format"] if responses else request["response_format"]["json_schema"]
        assert contract["strict"] is True
        assert_closed_output_schema(contract["schema"])
        schemas[contract["name"]] = contract["schema"]

    with model_service(responses=responses, outputs=[json.dumps(value) for value in outputs.values()], on_request=provider_check) as (url, requests):
        definition = project(tmp_path, url)
        application = definition.parents[1]
        if responses:
            change_model(tmp_path, adapter="openai_responses")
        for role, expected in outputs.items():
            value = yaml.safe_load((_RESOURCE_ROOT / f"{role}.yaml").read_text())
            schema = value["output_schema"]
            value.update(agent_runtime="pi", system_prompt=(_RESOURCE_ROOT / f"{role}.md").read_text(),
                         runtime_options={"compaction": {"enabled": False}, "transport": "sse", "cacheWarming": "off"})
            path = definition.parent / f"{role}.yaml"
            path.write_text(yaml.safe_dump(value))
            with bind_config(load_project_config(tmp_path)), Application.open(application, project_root=tmp_path, file_logging=False) as app:
                result = app.worker(f"workflows/{role}.yaml").invoke(WorkItem(role, {"evidence": {"probe1": "Observed evidence"}}))
                assert result.status == "success", result.to_dict()
                assert result.to_dict()["output"] == expected
            assert schemas[f"harness_{role}_output"] == schema
            validated = validate_role_output(role, result.output)
            if role in {"general", "domain", "compose"}:
                assert validated == {"edits": {item["path"]: item["content"] for item in expected["edits"]}, "rationale": expected["rationale"]}
            else:
                assert validated == expected
        assert len(requests) == len(outputs)
        assert all(path == ("/v1/responses" if responses else "/v1/chat/completions") for path, _, _ in requests)
