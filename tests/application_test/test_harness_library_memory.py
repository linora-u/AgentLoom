"""The public complete-task boundary owns ordered experiment Library visibility."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy

from tests.application_test.harness_model_support import role_of, text
from tests.application_test.mixed_runtime_support import model_service, project, write_yaml
from tests.application_test.test_harness_controller_application import DRIVER, real_isolation  # noqa: F401

pytestmark = pytest.mark.usefixtures("real_isolation")


LESSON = "LIBRARY-LESSON-142"


def _normalized(request):
    """Present either provider's actual request to the controlled model program."""
    if "messages" in request:
        return request
    messages = [{"role": "system", "content": request.get("instructions", "")}]
    for item in request.get("input", ()):
        if item.get("type") == "function_call_output":
            messages.append({"role": "tool", "content": item.get("output", "")})
        elif item.get("role"):
            messages.append(item)
    return {**request, "messages": messages}


def _application(tmp_path, url, monkeypatch, *, adapter="openai_chat"):
    root = tmp_path / "project"
    project(root, url, supervisor="pi", worker="pi")
    app = root / "applications/mixed"
    (app / "driver.py").write_text(DRIVER + "\nclass EvaluatorB(Evaluator):\n    pass\n")
    (app / "prompts").mkdir()
    (app / "prompts/business.md").write_text("BASE")
    models = yaml.safe_load((root / "config/model.yaml").read_text())
    for profile in models["model"].values():
        if isinstance(profile, dict):
            profile["adapter"] = adapter
    write_yaml(root / "config/model.yaml", models)
    write_yaml(app / "workflows/worker.yaml", {
        "name": "business", "agent_runtime": "pi", "model_type": "worker",
        "description": "Return a frozen supplied fact.", "task": "ORIGINAL_TASK: return fact.",
        "system_prompt": {"path": "prompts/business.md"}, "tools": [], "toolsets": [],
        "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}},
                         "required": ["fact"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"fact": {"type": "string"}, "variant": {"type": "string"}},
                          "required": ["fact", "variant"], "additionalProperties": False},
    })
    fact, hidden = tmp_path / "fact.txt", tmp_path / "heldout.txt"
    fact.write_text("FACT-123")
    hidden.write_text("HIDDEN-CANARY")
    monkeypatch.syspath_prepend(str(root))
    for key in tuple(sys.modules):
        if key.startswith("applications.mixed"):
            del sys.modules[key]
    return ApplicationRequest("mixed", root, app, "applications.mixed.driver:Driver",
                              task="ORIGINAL_TASK", input_paths=(fact,), hidden_paths=(hidden,),
                              driver_options={"delivery": str(tmp_path / "delivery.json")})


def test_historical_stream_reuses_real_library_without_business_memory_or_base_promotion(
    tmp_path, monkeypatch,
):
    phase = {"expect_library": False, "task_id": "2020-01-01"}
    read_evidence = []
    business_requests = []

    def program(raw):
        request = _normalized(raw)
        role = role_of(request)
        if role is None:
            body = json.dumps(request)
            assert LESSON not in body
            assert "harnesses/2020-01-01" not in body
            business_requests.append(request)
            system = text(request["messages"][0])
            return json.dumps({"fact": "FACT-123", "variant": "COMPOSED" if "COMPOSED" in system else "BASE"})
        tools = [item for item in request["messages"] if item.get("role") == "tool"]
        plan = [("read", {"path": "context/task.md"})]
        if role == "general":
            plan.append(("read", {"path": "context/harness/applications/mixed/prompts/business.md"}))
            plan.append(("read", {"path": "context/library/INDEX.md"}))
            if phase["expect_library"]:
                plan.extend(("read", {"path": path}) for path in (
                    "context/library/harnesses/2020-01-01/EXPERIENCE.md",
                    "context/library/harnesses/2020-01-01/harness/applications/mixed/prompts/business.md",
                ))
        if role == "compose":
            plan.append(("write", {"path": "context/harness/applications/mixed/prompts/business.md", "content": "COMPOSED"}))
        if len(tools) < len(plan):
            name, arguments = plan[len(tools)]
            return [(f"{role}-{len(tools)}", name, arguments)]
        evidence = "\n".join(text(item) for item in tools)
        assert "Permission denied" not in evidence
        if role == "general":
            assert "BASE" in text(tools[1]), "Each task must start from its declared base"
            if phase["expect_library"]:
                assert LESSON in evidence and "COMPOSED" in text(tools[-1])
                read_evidence.append(evidence)
            else:
                assert json.loads(text(tools[-1]))["status"] == "error", evidence
                assert LESSON not in evidence
        if role == "reflect":
            return ("<edits>component: business prompt\nchange: explicit output procedure\n"
                    "observed: BASE became COMPOSED in the final execution\nverdict: helped</edits>\n"
                    f"<experience>\ncue: {LESSON}\nlesson: Make the recurring output procedure explicit.\n"
                    "evidence: The changed business-prompt component changed the final variant.\n"
                    "confidence: high for the controlled fixture\n</experience>")
        return f"<notes>{role}: used current task evidence.</notes>"

    with model_service(program) as (url, requests):
        original = _application(tmp_path, url, monkeypatch)
        roots = tmp_path / "experiments"
        stream_root = tmp_path / "stream-library"
        base_policy = {"enabled": True, "recursion": {"mode": "parallel", "k": 1},
                       "feedback": {"mode": "development_score", "partition": "train-a",
                                    "evaluator": "applications.mixed.driver:Evaluator"}}

        def execute(name, task_id, *, mode="parallel", feedback="development_score", partition="train-a", evaluator="Evaluator", stream="one"):
            policy = HarnessingPolicy.model_validate({**base_policy,
                "recursion": {"mode": mode, "k": 1},
                "feedback": ({"mode": "development_score", "partition": partition,
                              "evaluator": "applications.mixed.driver:" + evaluator}
                             if feedback == "development_score" else {"mode": "contrastive"})})
            request = replace(original,
                task={"goal": "ORIGINAL_TASK", "evidence_cutoff": task_id + "T00:00:00Z"},
                driver_options={"delivery": str(tmp_path / (name + "-delivery.json"))},
                harness_stream={"id": stream, "task_ids": ["2020-01-01", "2020-01-02"],
                                "task_id": task_id, "root": str(stream_root)})
            return run_application(request, policy=policy, state_root=roots, experiment_id=name,
                                   environment={"PATH": os.environ.get("PATH", os.defpath)})

        first = execute("first", "2020-01-01")
        assert first.status == "completed", first.to_dict()
        assert first.library["submitted"] == 1
        first_entry = Path(first.library["path"]) / "harnesses/2020-01-01/EXPERIENCE.md"
        assert LESSON in first_entry.read_text()
        first_bytes = first_entry.read_bytes()
        phase.update(expect_library=True, task_id="2020-01-02")
        second = execute("second", "2020-01-02")
        assert second.status == "completed", second.to_dict()
        assert len(read_evidence) == 1
        assert second.library["path"] == first.library["path"]
        assert list(second.library["entries"]) == ["2020-01-01", "2020-01-02"]
        assert next(trial for trial in second.trials if trial.trial_id == "base1").output["variant"] == "BASE"
        assert second.output["variant"] == "COMPOSED"
        phase["expect_library"] = False
        paths = {first.library["path"]}
        for name, overrides in (
            ("mode", {"mode": "sequential"}),
            ("feedback", {"feedback": "contrastive"}),
            ("partition", {"partition": "train-b"}),
            ("evaluator", {"evaluator": "EvaluatorB"}),
            ("stream", {"stream": "other"}),
        ):
            isolated = execute(name, "2020-01-02", **overrides)
            assert isolated.status == "completed", isolated.to_dict()
            assert list(isolated.library["entries"]) == ["2020-01-02"]
            assert not list((roots / name / "roles").glob("*/workspace/context/library/INDEX.md"))
            assert isolated.library["path"] not in paths
            paths.add(isolated.library["path"])
            # A different stream's retain must never delete this stream's entries.
            assert first_entry.read_bytes() == first_bytes
        assert original.application_root.joinpath("prompts/business.md").read_text() == "BASE"
        assert all(trial.acceptance["hidden"] == "denied" for trial in second.trials)
        assert len(business_requests) == 28
        before = len(requests)
        resumed = run_application(
            replace(original, task={"goal": "ORIGINAL_TASK", "evidence_cutoff": "2020-01-02T00:00:00Z"},
                    driver_options={"delivery": str(tmp_path / "second-delivery.json")},
                    harness_stream={"id": "one", "task_ids": ["2020-01-01", "2020-01-02"],
                                    "task_id": "2020-01-02", "root": str(stream_root)}),
            policy=HarnessingPolicy.model_validate(base_policy), state_root=roots, resume_id="second",
            environment={"PATH": os.environ.get("PATH", os.defpath)},
        )
        assert resumed.status == "completed", resumed.to_dict()
        assert len(requests) == before
