"""Full Application Tasks use upstream freeform contracts over both Pi providers."""
from __future__ import annotations

import json
import os
from collections import Counter
from contextlib import contextmanager

import pytest
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.policy import HarnessingPolicy

from tests.application_test.harness_model_support import role_of
from tests.application_test.mixed_runtime_support import model_service
from tests.application_test.test_harness_controller_application import real_isolation  # noqa: F401
from tests.application_test.test_harness_library_memory import _application, _normalized
from tests.pi_test.test_application import model_service as pi_model_service

pytestmark = pytest.mark.usefixtures("real_isolation")


@contextmanager
def _provider_service(program, adapter):
    if adapter == "openai_responses":
        outputs = []
        with pi_model_service(responses=True, outputs=outputs,
                              on_request=lambda number, request: outputs.append(program(request))) as service:
            yield service
    else:
        with model_service(program) as service:
            yield service


@pytest.mark.parametrize("adapter", ["openai_chat", "openai_responses"])
def test_every_role_uses_upstream_text_contract_without_provider_edits_schema(
    tmp_path, monkeypatch, adapter,
):
    seen = Counter()
    wire = []

    def program(raw):
        request = _normalized(raw)
        role = role_of(request)
        if role is None:
            return json.dumps({"fact": "FACT-123", "variant": "BASE"})
        seen[role] += 1
        wire.append(raw)
        # Upstream owns each role's textual grammar. Model/provider configuration
        # must not force the removed whole-tree edits JSON contract back onto Pi.
        assert raw.get("response_format", {}).get("type") != "json_schema"
        assert raw.get("text", {}).get("format", {}).get("type") != "json_schema"
        assert "harness_general_output" not in json.dumps(raw)
        assert "harness_reflect_output" not in json.dumps(raw)
        names = {tool.get("name", tool.get("function", {}).get("name")) for tool in raw.get("tools", [])}
        assert {"read", "write", "edit"} <= names
        if role == "select":
            return "<why>The artifact satisfies the original frozen fact.</why>\n```choice\nA\n```"
        if role == "reflect":
            return "<edits>component: unchanged\nverdict: no observed effect</edits>\nNO_ENTRY"
        return f"<notes>{role}: KEEP_BASE; no supported edit in this controlled record.</notes>"

    with _provider_service(program, adapter) as (url, requests):
        request = _application(tmp_path, url, monkeypatch, adapter=adapter)
        policy = HarnessingPolicy.model_validate({"enabled": True, "recursion": {"mode": "parallel", "k": 2}})
        result = run_application(request, policy=policy, state_root=tmp_path / "experiments",
                                 experiment_id="role-contracts", environment={"PATH": os.environ.get("PATH", os.defpath)})
        assert result.status == "completed", result.to_dict()
        assert seen == {"general": 2, "domain": 2, "compose": 2, "select": 1, "reflect": 1}
        assert {role["role"] for role in result.roles} == set(seen)
        assert all(role["status"] == "completed" and isinstance(role["output"], str) for role in result.roles)
        assert len(result.trials) == 8
        assert result.output["fact"] == "FACT-123"
        assert result.library["submitted"] == 0
        assert request.application_root.joinpath("prompts/business.md").read_text() == "BASE"
        assert all(("input" in item) == (adapter == "openai_responses") for item in wire)
        count = len(requests)
        restored = run_application(request, policy=policy, state_root=tmp_path / "experiments",
                                   resume_id="role-contracts", environment={"PATH": os.environ.get("PATH", os.defpath)})
        assert restored.status == "completed", restored.to_dict()
        assert len(requests) == count
