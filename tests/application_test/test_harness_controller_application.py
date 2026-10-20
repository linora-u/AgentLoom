"""Complete-task tests cross real YAML Agents, HTTP models and OS isolation."""
from __future__ import annotations

import json
import os
import sys
from threading import Event, Thread
from time import monotonic, sleep
from pathlib import Path

import pytest

from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.isolation import IsolationUnavailable, preflight_isolation
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy
from tests.application_test.mixed_runtime_support import finish, model_service, project, write_yaml


@pytest.fixture
def real_isolation():
    try:
        return preflight_isolation()
    except IsolationUnavailable as error:
        pytest.skip(f"Real OS acceptance requires validated isolation: {error}")


DRIVER = """
import json
from pathlib import Path
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem
from agentloom.app.harnessing.models import PreparedInvocation, ExecutionReceipt, UsageObservation
from agentloom.app.harnessing.usage import read_run_usage

class Driver:
    def preflight(self, request, policy):
        pass
    def validate_candidate(self, invocation, project_root, application_root):
        import yaml
        value = yaml.safe_load((application_root / 'workflows/worker.yaml').read_text())
        if value['name'] != 'business' or 'ORIGINAL_TASK' not in value['task']:
            raise ValueError('fixed business purpose changed')
    def prepare(self, request, bundle):
        return PreparedInvocation(request, 'Return original frozen fact',
            {'purpose': 'ORIGINAL_TASK'}, {'required_fact': 'FACT-123'},
            {'fact_path': str(request.input_paths[0])})
    def reconcile(self, invocation, bundle, context):
        path = context.business_root / 'receipt.json'
        return ExecutionReceipt.from_dict(json.loads(path.read_text())) if path.exists() else None
    def execute_once(self, invocation, bundle, context):
        assert context.library_view == (), 'Business actors must not receive improvement library references'
        fact = Path(invocation.data['fact_path']).read_text()
        try:
            Path(invocation.request.hidden_paths[0]).read_text()
            hidden = 'UNEXPECTED_ALLOW'
        except PermissionError:
            hidden = 'denied'
        with Application.open(bundle.application_root, project_root=bundle.project_root, file_logging=False) as app:
            outcome = app.worker('workflows/worker.yaml').invoke(WorkItem(context.trial_id, {'fact': fact}))
            run = {'run_id': app.run.run_id, 'manifest_path': str(app.run.manifest_path), **outcome.to_dict()}
        output = outcome.output
        receipt = ExecutionReceipt(context.trial_id, bundle.candidate_id,
            'completed' if outcome.status == 'success' else 'failed', output,
            accepted=outcome.status == 'success' and output.get('fact') == 'FACT-123' and not (
                invocation.request.driver_options.get('reject_final') and context.trial_id.startswith('final')),
            acceptance={'hidden': hidden, 'fixed_fact': fact}, runs=(run,),
            usage=read_run_usage(Path(run['manifest_path']).parent),
            cleanup_complete=not outcome.cleanup_errors)
        context.business_root.mkdir(exist_ok=True)
        marker = context.business_root / 'before-receipt'
        if invocation.request.driver_options.get('stop_before_receipt') and context.trial_id == 'base1' and not marker.exists():
            import time
            marker.write_text('business done, receipt not published')
            while True:
                time.sleep(.01)
        (context.business_root / 'receipt.json').write_text(json.dumps(receipt.to_dict()))
        if invocation.request.driver_options.get('crash_after_completion') and context.trial_id == 'base1':
            import os
            os._exit(17)
        return receipt
    def publish(self, invocation, selected, decision_id):
        target = Path(invocation.request.driver_options['delivery'])
        if target.exists():
            old = json.loads(target.read_text())
            if old['decision_id'] != decision_id:
                raise ValueError('another delivery owns this path')
            return old
        value = {'decision_id': decision_id, 'selected': selected.trial_id, 'output': selected.to_dict()['output']}
        target.write_text(json.dumps(value))
        return value

class Evaluator:
    def evaluate(self, invocation, receipt, partition):
        return {'partition': partition, 'value': len(receipt.output.get('fact', '')),
                'explanation': 'DEV-CONFIGURED-SCORE', 'usage': []}
"""


@pytest.mark.parametrize(
    "mode,k,runtime,custom_roles,crash_after_completion,scenario",
    [
        ("parallel", 1, "smolagents", False, False, "normal"),
        ("parallel", 1, "smolagents", False, False, "stop_and_resume"),
        ("parallel", 1, "smolagents", False, False, "development_score"),
        ("parallel", 2, "smolagents", False, False, "selector_invalid"),
        ("parallel", 1, "smolagents", False, False, "final_rejected"),
        ("parallel", 1, "smolagents", False, False, "invalid_edits"),
        ("parallel", 1, "smolagents", False, True, "normal"),
        ("sequential", 2, "pi", True, False, "normal"),
    ],
)
def test_public_complete_task_executes_upstream_schedule_and_recovers_without_model_calls(
    tmp_path,
    monkeypatch,
    real_isolation,
    mode,
    k,
    runtime,
    custom_roles,
    crash_after_completion,
    scenario,
):
    requests_by_role = []

    def program(request):
        system = str(request["messages"][0]["content"])
        if request["model"] == "worker":
            result = {"fact": "FACT-123", "variant": "COMPOSED" if "COMPOSED" in system else "BASE"}
            role = "business"
        elif "Extract only supported reusable experiences" in system:
            result, role = {"experiences": []}, "reflect"
        elif "Select the deliverable that best satisfies" in system:
            result, role = {"chosen_id": "C1", "reason": "Uses the required frozen fact."}, "select"
        elif "Compose one coherent complete Application" in system:
            result, role = {
                "edits": {"applications/mixed/prompts/business.md": "COMPOSED"},
                "rationale": "Retain fixed fact and compose inspected probes.",
            }, "compose"
        elif "domain-specific working methods" in system:
            result, role = {"edits": {}, "rationale": "No supported domain edit."}, "domain"
        elif "general problem-solving" in system:
            result, role = {"edits": {}, "rationale": "No supported general edit."}, "general"
        else:
            raise AssertionError(system[:300])
        if scenario == "selector_invalid" and role == "select":
            result["chosen_id"] = "BASE"
        if scenario == "invalid_edits" and role in {"general", "domain", "compose"}:
            result["edits"] = {"applications/mixed/driver.py": "forbidden fixed business code change"}
        if role != "business":
            assert "HIDDEN-CANARY" not in json.dumps(request)
            if scenario == "development_score":
                assert "DEV-CONFIGURED-SCORE" in json.dumps(request)
        requests_by_role.append(role)
        return (
            finish(request, result)
            if any(t["function"]["name"] == "final_answer" for t in request.get("tools", []))
            else json.dumps(result)
        )

    with model_service(program) as (url, requests):
        root = tmp_path / "project"
        project(root, url, supervisor="smolagents", worker=runtime)
        app = root / "applications/mixed"
        (app / "driver.py").write_text(DRIVER)
        (app / "prompts").mkdir()
        (app / "prompts/business.md").write_text("BASE")
        write_yaml(
            app / "workflows/worker.yaml",
            {
                "name": "business",
                "agent_runtime": runtime,
                "model_type": "worker",
                "description": "Return a frozen supplied fact.",
                "task": "ORIGINAL_TASK: return fact.",
                "system_prompt": {"path": "prompts/business.md"},
                "tools": [],
                "toolsets": [],
                "input_schema": {
                    "type": "object",
                    "properties": {"fact": {"type": "string"}},
                    "required": ["fact"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"fact": {"type": "string"}, "variant": {"type": "string"}},
                    "required": ["fact", "variant"],
                    "additionalProperties": False,
                },
            },
        )
        roles = {}
        if custom_roles:
            import yaml

            resources = Path(__file__).parents[2] / "src/app/harnessing/resources/roles"
            for role in ("general", "domain", "compose", "select", "reflect"):
                value = yaml.safe_load((resources / f"{role}.yaml").read_text())
                value.update(agent_runtime=runtime, system_prompt=(resources / f"{role}.md").read_text())
                path = app / "workflows" / f"role_{role}.yaml"
                write_yaml(path, value)
                roles[role] = f"workflows/role_{role}.yaml"
        fact = tmp_path / "fact.txt"
        fact.write_text("FACT-123")
        hidden = tmp_path / "heldout.txt"
        hidden.write_text("HIDDEN-CANARY")
        monkeypatch.syspath_prepend(str(root))
        # Another parameter's module must not retain its previous project root.
        for key in tuple(sys.modules):
            if key.startswith("applications.mixed"):
                del sys.modules[key]
        request = ApplicationRequest(
            "mixed",
            root,
            app,
            "applications.mixed.driver:Driver",
            task="ORIGINAL_TASK",
            input_paths=(fact,),
            hidden_paths=(hidden,),
            driver_options={
                "delivery": str(tmp_path / "delivery.json"),
                "crash_after_completion": crash_after_completion,
                "reject_final": scenario == "final_rejected",
                "stop_before_receipt": scenario == "stop_and_resume",
            },
        )
        values = {"enabled": True, "recursion": {"mode": mode, "k": k}, "roles": roles}
        if scenario == "development_score":
            values["feedback"] = {
                "mode": "development_score",
                "partition": "development-a",
                "evaluator": "applications.mixed.driver:Evaluator",
            }
        policy = HarnessingPolicy.model_validate(values)
        environment = {"PATH": os.environ.get("PATH", os.defpath)}
        stop = Event()
        stopper = None
        if scenario == "stop_and_resume":

            def stop_after_actual_business():
                deadline = monotonic() + 30
                while monotonic() < deadline:
                    if list((tmp_path / "experiments").glob("*/trials/base1/business/before-receipt")):
                        stop.set()
                        return
                    sleep(0.01)

            stopper = Thread(target=stop_after_actual_business, daemon=True)
            stopper.start()
        result = run_application(
            request, policy=policy, state_root=tmp_path / "experiments", environment=environment, stop_token=stop
        )
        if stopper is not None:
            stopper.join(timeout=1)
            assert not stopper.is_alive()
            assert result.status == "stopped", result.to_dict()
            assert result.usage["total_tokens"] == 70
            assert result.usage["unmeasured_operations"]
            state = tmp_path / "experiments" / result.experiment_id
            cleanup = next((state / "trials/base1/process/attempts").glob("*/cleanup.json"))
            assert json.loads(cleanup.read_text())["physical_exit_confirmed"]
            stop.clear()
            result = run_application(
                request,
                policy=policy,
                state_root=tmp_path / "experiments",
                resume_id=result.experiment_id,
                environment=environment,
                stop_token=stop,
            )
        if crash_after_completion:
            assert result.status == "failed"
            assert result.usage["unmeasured_operations"]
            assert not result.usage["complete"]
            assert requests_by_role.count("business") == 1
            result = run_application(
                request,
                policy=policy,
                state_root=tmp_path / "experiments",
                resume_id=result.experiment_id,
                environment=environment,
            )
            assert requests_by_role.count("business") == 4 * k
        if scenario in {"selector_invalid", "final_rejected"}:
            assert result.status == "failed", result.to_dict()
            assert len(result.trials) == 4 * k
            assert any(t.trial_id.startswith("base") and t.eligible for t in result.trials)
            assert not result.selected_trial_id and result.output is None
            assert not (tmp_path / "delivery.json").exists()
            assert requests_by_role.count("reflect") == 0
            assert requests_by_role.count("select") == (scenario == "selector_invalid")
            return
        assert result.status == "completed", result.to_dict()
        assert len(result.trials) == 4 * k
        assert result.selected_trial_id.startswith("final")
        assert result.output == {"fact": "FACT-123", "variant": "BASE" if scenario == "invalid_edits" else "COMPOSED"}
        assert all(t.acceptance["hidden"] == "denied" for t in result.trials)
        assert requests_by_role.count("business") == 4 * k + (scenario == "stop_and_resume")
        edits = 2 * k if scenario == "invalid_edits" else k
        assert requests_by_role.count("general") == requests_by_role.count("domain") == edits
        assert requests_by_role.count("compose") == edits
        assert requests_by_role.count("select") == (k > 1)
        assert requests_by_role.count("reflect") == 1
        assert result.usage["complete"] == (
            runtime == "smolagents" and not crash_after_completion and scenario != "stop_and_resume"
        )
        assert result.usage["total_tokens"] == 70 * len(requests)
        for role_receipt in result.roles:
            observed = role_receipt["receipt"]["usage_observations"]
            assert observed and all(item["attempt_id"] for item in observed)
        state = tmp_path / "experiments" / result.experiment_id
        assert (state / "stages/publication.json").exists()
        if scenario == "development_score":
            evaluations = list((state / "stages").glob("evaluation.*.json"))
            assert len(evaluations) == 4 * k
            assert all(json.loads(path.read_text())["result"]["partition"] == "development-a" for path in evaluations)
        assert "fixture-key" not in "\n".join(p.read_text() for p in state.rglob("input.json"))
        fact.write_text("LIVE INPUT MUTATION")
        calls = len(requests)
        recovered = run_application(
            request,
            policy=policy,
            state_root=tmp_path / "experiments",
            resume_id=result.experiment_id,
            environment=environment,
        )
        assert recovered.to_dict() == result.to_dict()
        assert len(requests) == calls
        copied = state / "inputs/input-0"
        copied.write_text("TAMPERED FROZEN INPUT")
        with pytest.raises(ValueError, match="Frozen task input bytes changed"):
            run_application(
                request,
                policy=policy,
                state_root=tmp_path / "experiments",
                resume_id=result.experiment_id,
                environment=environment,
            )


def test_public_preparation_rejects_hidden_data_inside_an_input_root_before_model_calls(
    tmp_path, monkeypatch, real_isolation
):
    root = tmp_path / "project"
    project(root, "http://127.0.0.1:9/v1", supervisor="smolagents", worker="smolagents")
    app = root / "applications/mixed"
    (app / "driver.py").write_text(DRIVER)
    monkeypatch.syspath_prepend(str(root))
    for key in tuple(sys.modules):
        if key.startswith("applications.mixed"):
            del sys.modules[key]
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "public.txt").write_text("FACT-123")
    hidden = facts / "heldout.txt"
    hidden.write_text("HIDDEN SCORE")
    request = ApplicationRequest(
        "mixed", root, app, "applications.mixed.driver:Driver", input_paths=(facts,), hidden_paths=(hidden,)
    )
    with pytest.raises(ValueError, match="input root overlaps hidden"):
        run_application(request, policy=HarnessingPolicy(enabled=True), state_root=tmp_path / "experiments")
    assert not list((tmp_path / "experiments").rglob("process/input.json"))


def test_public_missing_ordinary_role_rejects_before_any_trial(tmp_path, monkeypatch, real_isolation):
    root = tmp_path / "project"
    project(root, "http://127.0.0.1:9/v1", supervisor="smolagents", worker="smolagents")
    app = root / "applications/mixed"
    (app / "driver.py").write_text(DRIVER)
    write_yaml(app / "workflows/worker.yaml", {"name": "business", "task": "ORIGINAL_TASK"})
    monkeypatch.syspath_prepend(str(root))
    for key in tuple(sys.modules):
        if key.startswith("applications.mixed"):
            del sys.modules[key]
    fact = tmp_path / "fact.txt"
    fact.write_text("FACT-123")
    request = ApplicationRequest("mixed", root, app, "applications.mixed.driver:Driver", input_paths=(fact,))
    with pytest.raises((ValueError, FileNotFoundError), match="missing|exist|Missing|No such"):
        run_application(
            request,
            policy=HarnessingPolicy.model_validate({"enabled": True, "roles": {"general": "workflows/missing.yaml"}}),
            state_root=tmp_path / "experiments",
        )
    assert not list((tmp_path / "experiments").rglob("process/input.json"))


def test_public_unsupported_isolation_rejects_before_any_model_or_trial(tmp_path, monkeypatch):
    from agentloom.app.harnessing import isolation

    with model_service(lambda request: pytest.fail("Unsupported backend must not call a model")) as (url, requests):
        root = tmp_path / "project"
        project(root, url, supervisor="smolagents", worker="smolagents")
        app = root / "applications/mixed"
        (app / "driver.py").write_text(DRIVER)
        monkeypatch.syspath_prepend(str(root))
        for key in tuple(sys.modules):
            if key.startswith("applications.mixed"):
                del sys.modules[key]

        def unavailable():
            raise IsolationUnavailable("fixture lacks validated OS isolation")

        monkeypatch.setattr(isolation, "preflight_isolation", unavailable)
        request = ApplicationRequest("mixed", root, app, "applications.mixed.driver:Driver")
        with pytest.raises(IsolationUnavailable, match="lacks validated OS"):
            run_application(request, policy=HarnessingPolicy(enabled=True), state_root=tmp_path / "experiments")
        assert requests == []
        assert not list((tmp_path / "experiments").rglob("process/input.json"))
