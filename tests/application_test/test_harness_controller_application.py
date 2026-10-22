"""Complete-task tests cross real YAML Agents, HTTP models and OS isolation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Barrier, Event, Thread
from time import monotonic, sleep

import pytest
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.isolation import IsolationUnavailable, preflight_isolation
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy

from tests.application_test.harness_model_support import HarnessModel
from tests.application_test.mixed_runtime_support import (
    finish,
    model_service,
    project,
    write_yaml,
)


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
                invocation.request.driver_options.get('reject_final') and context.trial_id.startswith(('agg', 'c'))),
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
        marker = invocation.request.driver_options.get('evaluation_failure_marker')
        if marker and not Path(marker).exists():
            Path(marker).write_text('evaluation failed after business completion')
            raise RuntimeError('Controlled evaluator failure')
        if invocation.request.driver_options.get('unscorable'):
            return {'partition': partition, 'status': 'unscorable',
                    'metrics': {}, 'sample_count': 0, 'usage': []}
        return {'partition': partition, 'status': 'scored',
                'metrics': {'DEV-CONFIGURED-SCORE': len(receipt.output.get('fact', ''))},
                'sample_count': 1, 'usage': []}
"""


@pytest.mark.parametrize("identity", ["", False, "../escape", []])
def test_invalid_reserved_identity_is_rejected_before_experiment_creation(tmp_path, identity):
    request = ApplicationRequest("example", tmp_path, tmp_path / "applications/example", "example:Driver", task="task")
    with pytest.raises(ValueError, match="Invalid experiment"):
        run_application(request, experiment_id=identity, state_root=tmp_path / "experiments")
    assert not (tmp_path / "experiments").exists()


def test_new_identity_and_resume_identity_are_mutually_exclusive(tmp_path):
    request = ApplicationRequest("example", tmp_path, tmp_path / "applications/example", "example:Driver", task="task")
    with pytest.raises(ValueError, match="either"):
        run_application(request, experiment_id="new", resume_id="old", state_root=tmp_path / "experiments")
    assert not (tmp_path / "experiments").exists()


@pytest.mark.parametrize(
    "mode,k,runtime,custom_roles,crash_after_completion,scenario",
    [
        ("parallel", 1, "smolagents", False, False, "normal"),
        ("parallel", 1, "smolagents", False, False, "stop_and_resume"),
        ("parallel", 1, "smolagents", False, False, "stop_after_final"),
        ("parallel", 1, "pi", False, False, "stop_after_final"),
        ("parallel", 1, "smolagents", False, False, "development_score"),
        ("parallel", 1, "pi", False, False, "evaluation_recovery"),
        ("parallel", 1, "pi", False, False, "role_commit_recovery"),
        ("parallel", 1, "pi", False, False, "failed_role_commit_recovery"),
        ("parallel", 1, "pi", False, False, "role_timeout"),
        ("parallel", 1, "pi", False, False, "cached_receipt_mismatch"),
        ("parallel", 1, "pi", False, False, "unscorable"),
        ("parallel", 2, "smolagents", False, False, "selector_invalid"),
        ("parallel", 2, "pi", False, False, "normal"),
        ("parallel", 1, "pi", False, False, "large_evidence"),
        ("parallel", 1, "pi", False, False, "skill_edit"),
        ("parallel", 1, "pi", False, False, "resource_discovery"),
        ("parallel", 1, "pi", False, False, "reflection_error"),
        ("parallel", 1, "pi", False, False, "reflection_invalid"),
        ("parallel", 1, "smolagents", False, False, "final_rejected"),
        ("parallel", 1, "smolagents", False, False, "invalid_edits"),
        ("parallel", 1, "smolagents", False, False, "unsafe_tools"),
        ("parallel", 1, "smolagents", False, False, "registry_tools"),
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
    from uuid import uuid4

    skill_name = "business-behavior" + ("-" + uuid4().hex if scenario == "resource_discovery" else "")
    skill_relative = f"applications/mixed/skills/{skill_name}/SKILL.md"
    skill_baseline = f"---\nname: {skill_name}\ndescription: Read the business behavior marker.\n---\nBASE-SKILL\n"
    skill_composed = skill_baseline.replace("BASE-SKILL", "COMPOSED-SKILL")
    harness = HarnessModel("applications/mixed/prompts/business.md", scenario=scenario,
                           feedback=scenario in {"development_score", "evaluation_recovery", "unscorable"})
    if scenario == "role_commit_recovery":
        from agentloom.app.harnessing import process
        atomic = process._atomic
        interrupted_commit = False
        def lose_controller_before_role_commit(path, value):
            nonlocal interrupted_commit
            if not interrupted_commit and path.name == "result.json" and path.parent.name == "process" and "roles" in path.parts:
                interrupted_commit = True
                raise KeyboardInterrupt("controlled controller loss before role result commit")
            return atomic(path, value)
        monkeypatch.setattr(process, "_atomic", lose_controller_before_role_commit)
    if scenario == "role_timeout":
        from agentloom.app.harnessing import process
        execute = process._execute
        harness.pause = Event()
        def timeout_first_role(bundle, context, factory, payload, **options):
            if factory.endswith(":execute_role") and payload["role"] == "general" and payload["stage"].endswith(".t1"):
                options["timeout"] = 5
                try:
                    return execute(bundle, context, factory, payload, **options)
                finally:
                    harness.pause.set()
            return execute(bundle, context, factory, payload, **options)
        monkeypatch.setattr(process, "_execute", timeout_first_role)

    if (mode, k, runtime, custom_roles, scenario) == ("parallel", 2, "pi", False, "normal"):
        # Identical independent compositions must share one immutable bundle
        # without consuming another model attempt when publication races.
        rendezvous = Barrier(2)
        rename = os.rename

        def concurrent_candidate_commit(source, destination, *args, **kwargs):
            source_path = Path(source)
            prompt = source_path / "project/applications/mixed/prompts/business.md"
            if source_path.name.startswith(".candidate-") and prompt.is_file() and prompt.read_text() == "COMPOSED":
                rendezvous.wait(timeout=30)
            return rename(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "rename", concurrent_candidate_commit)

    def program(request):
        answer = harness(request)
        if answer is not None:
            return answer
        assert request["model"] == "worker"
        system = str(request["messages"][0]["content"])
        assert all(tool["function"]["name"] != "expose_private" for tool in request.get("tools", []))
        if scenario == "registry_tools" and len(requests_by_role) == 3:
            assert any(tool["function"]["name"] == "read_file" for tool in request.get("tools", []))
        if scenario in {"skill_edit", "resource_discovery"}:
            import re
            from html import unescape

            from tests.application_test.harness_model_support import text

            assert "BASE-SKILL" not in system and "COMPOSED-SKILL" not in system
            locations = re.findall(r"<location>([^<]+/skills/[^/<]+/SKILL\.md)</location>", system)
            assert len(locations) == 1, system
            tool_results = [message for message in request["messages"] if message["role"] == "tool"]
            if not tool_results:
                return [("read-declared-skill", "read", {"path": unescape(locations[0])})]
            skill_content = text(tool_results[-1])
            assert "COMPOSED-SKILL" in skill_content or "BASE-SKILL" in skill_content, skill_content
            variant = "COMPOSED" if "COMPOSED-SKILL" in skill_content else "BASE"
        else:
            variant = "COMPOSED" if "COMPOSED" in system else "BASE"
        result = {"fact": "FACT-123", "variant": variant}
        requests_by_role.append("business")
        return finish(request, result) if any(
            tool["function"]["name"] == "final_answer" for tool in request.get("tools", [])
        ) else json.dumps(result)

    with model_service(program) as (url, requests):
        root = tmp_path / "project"
        project(root, url, supervisor="smolagents", worker=runtime)
        app = root / "applications/mixed"
        if not custom_roles:
            # Built-ins inherit the Application's default, including a model
            # profile absent from the project catalog.
            write_yaml(app / "config/model.yaml", {"model": {
                "default_model_type": "improvement",
                "improvement": {"adapter": "openai_chat", "model": "improvement",
                                "base_url": url, "api_key": "fixture-key", "timeout": 20,
                                "num_retries": 0, "supports_structured_output": True}}})
        (app / "driver.py").write_text(DRIVER)
        (app / "prompts").mkdir()
        (app / "prompts/business.md").write_text("BASE")
        (app / "prompts/obsolete.md").write_text("UNUSED RESOURCE")
        if scenario == "large_evidence":
            # A real Application may contain a multi-megabyte reference file.
            # Every built-in Pi role must still execute, rather than silently
            # inheriting the base after its JSONL bridge rejects the input.
            (app / "prompts/large.md").write_text(("中文证据😀 " + "x" * 1000 + "\n") * 9000 + "END-OF-COMPLETE-EVIDENCE\n")
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
        if scenario in {"skill_edit", "resource_discovery"}:
            import yaml

            skill = root / skill_relative
            skill.parent.mkdir(parents=True)
            skill.write_text(skill_baseline)
            worker = yaml.safe_load((app / "workflows/worker.yaml").read_text())
            worker.update(skills={"paths": [f"skills/{skill_name}"]}, tools=[{"name": "read"}])
            write_yaml(app / "workflows/worker.yaml", worker)
            if scenario == "skill_edit":
                harness.candidate_override = (skill_relative, skill_composed)
        if scenario in {"unsafe_tools", "registry_tools"}:
            import yaml
            worker = yaml.safe_load((app / "workflows/worker.yaml").read_text())
            worker["tools"] = ([{"name": "expose_private", "module": "agentloom.config.private_bindings",
                                "function": "resolve_bound_credentials"}]
                               if scenario == "unsafe_tools" else [{"name": "read_file"}])
            harness.candidate_override = ("applications/mixed/workflows/worker.yaml", yaml.safe_dump(worker))
        roles = {}
        if custom_roles:
            import yaml

            resources = Path(__file__).parents[2] / "src/app/harnessing/resources/roles"
            for role in ("general", "domain", "compose", "select", "reflect"):
                value = yaml.safe_load((resources / f"{role}.yaml").read_text())
                value.update(agent_runtime="pi", tools=[{"name": name} for name in ("read", "write", "edit")])
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
                "evaluation_failure_marker": str(tmp_path / "evaluation.marker") if scenario == "evaluation_recovery" else None,
                "unscorable": scenario == "unscorable",
            },
        )
        values = {"enabled": True, "recursion": {"mode": mode, "k": k}, "roles": roles}
        if harness.feedback:
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
        if scenario == "failed_role_commit_recovery":
            script = '''
import json, os, sys
from pathlib import Path
from agentloom.app.harnessing import process
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy
atomic = process._atomic
def crash_after_failed_role_commit(path, value):
    atomic(path, value)
    if (path.name == 'result.json' and path.parent.name == 'process'
            and 'roles' in path.parts and value.get('result', {}).get('status') == 'failed'):
        os._exit(91)
process._atomic = crash_after_failed_role_commit
value = json.load(sys.stdin)
run_application(ApplicationRequest.from_dict(value['request']),
    policy=HarnessingPolicy.model_validate(value['policy']), state_root=Path(value['state_root']),
    environment=value['environment'], experiment_id='preallocated-experiment')
'''
            controller = subprocess.run([sys.executable, "-c", script], input=json.dumps({
                "request": request.to_dict(), "policy": policy.model_dump(mode="json"),
                "state_root": str(tmp_path / "experiments"), "environment": environment}),
                env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root), os.environ.get("PYTHONPATH", "")])},
                capture_output=True, text=True, timeout=30)
            assert controller.returncode == 91, controller.stderr
            orphan = next((tmp_path / "experiments/preallocated-experiment/roles").glob("*/process/result.json"))
            assert json.loads(orphan.read_text())["result"]["status"] == "failed"
            assert not (orphan.parent.parent / "receipt.json").exists()
            result = run_application(request, state_root=tmp_path / "experiments", environment=environment,
                resume_id="preallocated-experiment")
        else:
            result = run_application(
                request, policy=policy, state_root=tmp_path / "experiments", environment=environment, stop_token=stop,
                experiment_id="preallocated-experiment",
                events=(lambda event: stop.set() if event["event"] == "trial.completed"
                        and event.get("trial_id", "").startswith(("agg", "c")) else None) if scenario in {"stop_after_final", "cached_receipt_mismatch"} else None,
            )
        assert result.experiment_id == "preallocated-experiment"
        if scenario == "role_commit_recovery":
            assert result.status == "stopped", result.to_dict()
            assert harness.sessions["general"] == 1
            assert requests_by_role.count("business") == 1
            result = run_application(request, state_root=tmp_path / "experiments",
                environment=environment, resume_id=result.experiment_id)
            assert harness.sessions["general"] == 1, "Completed durable Pi role was paid for again"
        if scenario == "evaluation_recovery":
            assert result.status == "partial", result.to_dict()
            assert "Controlled evaluator failure" in "\n".join(result.errors)
            assert requests_by_role.count("business") == 1
            assert not harness.sessions
            assert not (tmp_path / "delivery.json").exists()
            result = run_application(request, state_root=tmp_path / "experiments",
                environment=environment, resume_id=result.experiment_id)
            assert requests_by_role.count("business") == 4
        if scenario in {"stop_after_final", "cached_receipt_mismatch"}:
            assert result.status == "stopped", result.to_dict()
            assert len(result.trials) == 4
            assert not result.delivery and not (tmp_path / "delivery.json").exists()
            assert harness.sessions["reflect"] == harness.sessions["select"] == 0
            assert not (tmp_path / "experiments" / result.experiment_id / "stages/publication.json").exists()
            business_calls = requests_by_role.count("business")
            assert business_calls == 4
            if scenario == "cached_receipt_mismatch":
                receipt_path = tmp_path / "experiments" / result.experiment_id / "trials/inv1/receipt.json"
                changed = json.loads(receipt_path.read_text())
                changed["output"] = {"fact": "TAMPERED-NONSELECTED-RECEIPT"}
                receipt_path.write_text(json.dumps(changed))
                calls = len(requests)
                stop.clear()
                rejected = run_application(request, state_root=tmp_path / "experiments",
                    environment=environment, resume_id=result.experiment_id, stop_token=stop)
                assert rejected.status == "failed", rejected.to_dict()
                assert "Trial receipt differs from its completed host execution record" in "\n".join(rejected.errors)
                assert not rejected.delivery and not (tmp_path / "delivery.json").exists()
                assert len(requests) == calls and requests_by_role.count("business") == business_calls
                return
            (app / "workflows/worker.yaml").write_text("invalid: [")
            (app / "prompts/business.md").write_text("LIVE-MUTATION")
            (root / "config/system.yaml").write_text("invalid: [")
            import yaml
            changed_models = yaml.safe_load((root / "config/model.yaml").read_text())
            for model in changed_models["model"].values():
                if isinstance(model, dict):
                    model["base_url"] = "http://127.0.0.1:9/v1"
            write_yaml(root / "config/model.yaml", changed_models)
            stop.clear()
            result = run_application(request, state_root=tmp_path / "experiments",
                environment=environment, resume_id=result.experiment_id, stop_token=stop)
            assert requests_by_role.count("business") == business_calls
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
            assert result.status in {"failed", "partial"}, result.to_dict()
            assert len(result.trials) == 4 * k
            assert any(t.trial_id.startswith("base") and t.eligible for t in result.trials)
            assert not result.selected_trial_id and result.output is None
            assert not (tmp_path / "delivery.json").exists()
            assert harness.sessions["reflect"] == 0
            assert harness.sessions["select"] == (scenario == "selector_invalid")
            return
        assert result.status == "completed", result.to_dict()
        assert len(result.trials) == 4 * k
        assert result.selected_trial_id.startswith(("agg", "c"))
        assert result.output == {
            "fact": "FACT-123",
            "variant": "BASE" if scenario in {"invalid_edits", "unsafe_tools", "registry_tools"} else "COMPOSED",
        }
        assert all(t.acceptance["hidden"] == "denied" for t in result.trials)
        assert requests_by_role.count("business") == 4 * k + (scenario == "stop_and_resume")
        assert harness.sessions["general"] == k + (scenario in {"role_timeout", "failed_role_commit_recovery"})
        if scenario == "failed_role_commit_recovery":
            # The real controller can die while its parallel peer is still
            # running. That peer may need a fresh session after owned cleanup.
            assert k <= harness.sessions["domain"] <= k + 1
        else:
            assert harness.sessions["domain"] == k
        assert harness.sessions["compose"] == (2 * k if scenario in {"invalid_edits", "unsafe_tools"} else k)
        assert harness.sessions["select"] == (k > 1)
        assert harness.sessions["reflect"] == (2 if scenario == "reflection_error" else 1)
        # Built-in roles now use Pi even when business execution uses smol.
        # This fixture's Pi adapter does not report every usage dimension.
        assert result.usage["complete"] is False
        if scenario in {"role_timeout", "failed_role_commit_recovery"}:
            # Killing a role or its parallel peer before the Runtime closes
            # can leave HTTP usage unreported. Preserve unknown operations
            # rather than infer usage from this server's fulfilled requests.
            assert 0 < result.usage["total_tokens"] <= 70 * len(requests)
        else:
            measured_requests = len(requests) - (2 if scenario == "reflection_error" else 0)
            assert result.usage["total_tokens"] == 70 * measured_requests
        if scenario == "reflection_error":
            failed = [role for role in result.roles if role["role"] == "reflect"]
            assert len(failed) == 2 and all(role["status"] == "failed" for role in failed)
            assert all(not item["complete"] for role in failed for item in role["receipt"]["usage_observations"])
            assert result.library["submitted"] == 0
        for role_receipt in result.roles:
            observed = role_receipt["receipt"]["usage_observations"]
            assert observed and all(item["attempt_id"] for item in observed)
        state = tmp_path / "experiments" / result.experiment_id
        if scenario in {"skill_edit", "resource_discovery"}:
            assert {trial.trial_id: trial.output["variant"] for trial in result.trials} == {
                "base1": "BASE", "inv1": "BASE", "dom1": "BASE", "agg1": "COMPOSED"}
            selected = next(trial for trial in result.trials if trial.trial_id == result.selected_trial_id)
            assert result.selected_candidate_id == selected.candidate_id
            assert result.output == selected.output
            assert (root / skill_relative).read_text() == skill_baseline
            for trial in result.trials:
                bundle = state / "bundles" / trial.candidate_id.removeprefix("sha256:") / "project"
                expected = skill_composed if trial.trial_id == "agg1" else skill_baseline
                assert (bundle / skill_relative).read_text() == expected
                assert (bundle / "applications/mixed/prompts/business.md").read_text() == "BASE"
                run_dir = Path(trial.runs[0]["manifest_path"]).parent
                records = [event["details"]["record"] for line in (run_dir / "audit/runtime_events.jsonl").read_text().splitlines()
                           if (event := json.loads(line))["kind"] == "tool"]
                assert len(records) == 1
                assert records[0]["input"] == {"path": str(bundle / skill_relative)}
                assert records[0]["tool_name"] == "read" and records[0]["status"] == "completed"
                assert expected.strip() in json.dumps(records[0]["output"], ensure_ascii=False).replace("\\n", "\n")
            for role in result.roles:
                if role["role"] not in ({"general", "domain", "compose"} if scenario == "resource_discovery" else {"compose"}):
                    continue
                run_dir = Path(role["receipt"]["run"]["manifest_path"]).parent
                records = [event["details"]["record"] for line in (run_dir / "audit/runtime_events.jsonl").read_text().splitlines()
                           if (event := json.loads(line))["kind"] == "tool"]
                expected = {("read", "completed")}
                if role["role"] == "compose":
                    expected.add(("edit" if scenario == "resource_discovery" else "write", "completed"))
                assert {(record["tool_name"], record["status"]) for record in records
                        if record["input"].get("path") == "context/harness/" + skill_relative} == expected
                if scenario == "resource_discovery":
                    assert all(record["status"] == "completed" for record in records)
                    assert any(record["tool_name"] == "read" and "BASE-SKILL" in json.dumps(record["output"])
                               for record in records if record["input"].get("path") == "context/harness/" + skill_relative)
        if scenario == "failed_role_commit_recovery":
            failed = [role for role in result.roles if role["role"] == "general" and role["status"] == "failed"]
            assert len(failed) == 1 and failed[0]["receipt"]["usage_observations"]
            for observed in failed[0]["receipt"]["usage_observations"]:
                restored = next(item for item in result.usage["observations"]
                    if (item["invocation_id"], item["attempt_id"]) == (observed["invocation_id"], observed["attempt_id"]))
                assert restored["total_tokens"] == observed["total_tokens"] == 70
            if harness.sessions["domain"] > k:
                assert any(item["operation_id"].startswith("pi.dom1.")
                    for item in result.usage["unmeasured_operations"])
            from agentloom.execution.managed_process import recover_owned_process
            assert all(recover_owned_process(resource).observe()["physical_exit_confirmed"]
                for resource in (state / "roles").glob("*/process/attempts/*/resource"))
        if scenario == "role_timeout":
            retries = sorted((state / "roles").glob("pi.inv1.*"))
            assert [path.name.rsplit(".", 1)[-1] for path in retries] == ["t1", "t2"]
            assert all(json.loads(path.read_text())["physical_exit_confirmed"]
                for path in retries[0].glob("process/attempts/*/cleanup.json"))
            assert (retries[0] / "workspace/context/harness/applications/mixed/prompts/business.md").read_text() == "FAILED-ATTEMPT-EDIT"
            assert (retries[1] / "workspace/context/harness/applications/mixed/prompts/business.md").read_text() == "BASE"
            assert result.usage["unmeasured_operations"]
        if scenario == "large_evidence":
            assert any(role == "general" and path.endswith("large.md") for role, _name, path in harness.reads)
            assert all(len(json.dumps(item).encode()) < 512 * 1024 for item in requests)
        assert (state / "stages/publication.json").exists()
        if scenario == "normal":
            selected = next(trial for trial in result.trials if trial.trial_id == result.selected_trial_id)
            assert selected.candidate_id != next(trial for trial in result.trials if trial.trial_id == "base1").candidate_id
        if harness.feedback:
            evaluations = list((state / "stages").glob("evaluation.*.json"))
            assert len(evaluations) == 4 * k
            assert {"general", "domain", "compose", "reflect"} <= set(harness.seen_feedback)
            assert all(json.loads(path.read_text())["result"]["partition"] == "development-a" for path in evaluations)
            if scenario == "unscorable":
                assert all(json.loads(path.read_text())["result"]["status"] == "unscorable" for path in evaluations)
        assert "fixture-key" not in "\n".join(p.read_text() for p in state.rglob("input.json"))
        fact.write_text("LIVE INPUT MUTATION")
        (root / "config/system.yaml").write_text("invalid: [")
        (root / "config/model.yaml").write_text("invalid: [")
        calls = len(requests)
        recovered = run_application(
            request,
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
