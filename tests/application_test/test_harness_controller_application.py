"""Complete-task tests cross real YAML Agents, HTTP models and OS isolation."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy
from tests.application_test.mixed_runtime_support import finish, model_service, project, write_yaml


DRIVER = """
import json
from pathlib import Path
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem
from agentloom.app.harnessing.models import PreparedInvocation, ExecutionReceipt, UsageObservation

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
            accepted=outcome.status == 'success' and output.get('fact') == 'FACT-123',
            acceptance={'hidden': hidden, 'fixed_fact': fact}, runs=(run,),
            usage=(UsageObservation(outcome.invocation_id, **{k:outcome.usage.get(k)
                for k in ('input_tokens','output_tokens','total_tokens')}, complete=False),),
            cleanup_complete=not outcome.cleanup_errors)
        context.business_root.mkdir(exist_ok=True)
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
"""


@pytest.mark.parametrize(
    "mode,k,runtime,custom_roles,crash_after_completion",
    [
        ("parallel", 1, "smolagents", False, False),
        ("parallel", 1, "smolagents", False, True),
        ("sequential", 2, "pi", True, False),
    ],
)
def test_public_complete_task_executes_upstream_schedule_and_recovers_without_model_calls(
    tmp_path,
    monkeypatch,
    mode,
    k,
    runtime,
    custom_roles,
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
            },
        )
        policy = HarnessingPolicy.model_validate({"enabled": True, "recursion": {"mode": mode, "k": k}, "roles": roles})
        environment = {"PATH": "/home/lin/.nvm/versions/node/v22.23.3/bin:" + os.defpath}
        result = run_application(request, policy=policy, state_root=tmp_path / "experiments", environment=environment)
        if crash_after_completion:
            assert result.status == "failed"
            assert requests_by_role.count("business") == 1
            result = run_application(
                request,
                policy=policy,
                state_root=tmp_path / "experiments",
                resume_id=result.experiment_id,
                environment=environment,
            )
            assert requests_by_role.count("business") == 4 * k
        assert result.status == "completed", result.to_dict()
        assert len(result.trials) == 4 * k
        assert result.selected_trial_id.startswith("final")
        assert result.output == {"fact": "FACT-123", "variant": "COMPOSED"}
        assert all(t.acceptance["hidden"] == "denied" for t in result.trials)
        assert requests_by_role.count("business") == 4 * k
        assert requests_by_role.count("general") == requests_by_role.count("domain") == k
        assert requests_by_role.count("compose") == k
        assert requests_by_role.count("select") == (k > 1)
        assert requests_by_role.count("reflect") == 1
        assert not result.usage["complete"]
        assert result.usage["total_tokens"] > 0
        state = tmp_path / "experiments" / result.experiment_id
        assert (state / "stages/publication.json").exists()
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
