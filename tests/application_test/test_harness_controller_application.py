"""Complete-task tests cross real YAML Agents, HTTP models and OS isolation."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from threading import Barrier, Event, Thread
from time import monotonic, sleep

import pytest
from agentloom.app.harnessing.controller import run_application
from agentloom.app.harnessing.isolation import IsolationUnavailable, preflight_isolation
from agentloom.app.harnessing.models import ApplicationRequest
from agentloom.app.harnessing.policy import HarnessingPolicy

from tests.application_test.mixed_runtime_support import (
    assert_closed_output_schema,
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
        ("parallel", 2, "smolagents", False, False, "selector_invalid"),
        ("parallel", 2, "pi", False, False, "normal"),
        ("parallel", 1, "pi", False, False, "large_evidence"),
        ("parallel", 1, "smolagents", False, False, "final_rejected"),
        ("parallel", 1, "smolagents", False, False, "invalid_edits"),
        ("parallel", 1, "pi", True, False, "duplicate_edits"),
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
    evidence_reads = []

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

    def message_text(message):
        content = message["content"]
        if isinstance(content, str):
            return content
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))

    def program(request):
        if custom_roles and runtime == "pi" and request["model"] == "supervisor":
            contract = request["response_format"]["json_schema"]
            assert contract["strict"] is True

            assert_closed_output_schema(contract["schema"])
        system = str(request["messages"][0]["content"])
        if request["model"] == "worker":
            assert all(tool["function"]["name"] != "expose_private" for tool in request.get("tools", []))
            if scenario == "registry_tools" and len([role for role in requests_by_role if role == "business"]) == 3:
                assert any(tool["function"]["name"] == "read_file" for tool in request.get("tools", []))
            result = {"fact": "FACT-123", "variant": "COMPOSED" if "COMPOSED" in system else "BASE"}
            role = "business"
        elif "Extract only supported reusable experiences" in system:
            result, role = {"experiences": []}, "reflect"
        elif "Select the deliverable that best satisfies" in system:
            result, role = {"chosen_id": "C1", "reason": "Uses the required frozen fact."}, "select"
        elif "Compose one coherent complete Application" in system:
            result, role = {
                "edits": [{"path": "applications/mixed/prompts/business.md", "content": "COMPOSED"}],
                "rationale": "Retain fixed fact and compose inspected probes.",
            }, "compose"
        elif "domain-specific working methods" in system:
            result, role = {"edits": [], "rationale": "No supported domain edit."}, "domain"
        elif "general problem-solving" in system:
            result, role = {"edits": [], "rationale": "No supported general edit."}, "general"
        else:
            raise AssertionError(system[:300])
        if scenario == "large_evidence" and role == "general":
            tool_results = [message for message in request["messages"] if message["role"] == "tool"]
            if not tool_results:
                task = next(message_text(message) for message in request["messages"]
                            if message["role"] == "user" and "Input data (JSON):\n" in message_text(message))
                evidence = json.loads(task.split("Input data (JSON):\n", 1)[1])["evidence"]
                resource_root = Path(evidence["resource_root"])
                assert resource_root != root
                assert resource_root.name == "project" and resource_root.parent.parent.name == "bundles"
                evidence_reads.append("index")
                return [("read-index", "read", {"path": evidence["evidence_file"]})]
            if len(tool_results) == 1:
                ref = re.search(r"ctx_[0-9a-f]{32}", message_text(tool_results[0]))
                assert ref is not None
                assert any(tool["function"]["name"] == "loom_retrieve_context"
                           for tool in request["tools"])
                evidence_reads.append("retrieve-index")
                return [("retrieve-index", "loom_retrieve_context", {"ref": ref.group()})]
            if len(tool_results) == 2:
                index_text = message_text(tool_results[1])
                assert "next_offset=none" in index_text
                index = json.loads(index_text.split("\n", 1)[1])
                parts = index["files"]["applications/mixed/prompts/large.md"]["harness_text_parts"]
                evidence_reads.append("tail")
                return [("read-tail", "read", {
                    "path": parts["path_template"].format(index=parts["count"] - 1)})]
            assert "END-OF-COMPLETE-EVIDENCE" in message_text(tool_results[-1])
        if scenario == "selector_invalid" and role == "select":
            result["chosen_id"] = "BASE"
        if scenario == "invalid_edits" and role in {"general", "domain", "compose"}:
            result["edits"] = [{"path": "applications/mixed/driver.py", "content": "forbidden fixed business code change"}]
        if scenario == "duplicate_edits" and role in {"general", "domain", "compose"}:
            result["edits"] = [{"path": "applications/mixed/prompts/business.md", "content": text}
                               for text in ("AMBIGUOUS-FIRST", "AMBIGUOUS-LAST")]
        if scenario == "normal" and mode == "parallel" and role == "compose":
            result["edits"].extend([
                {"path": "applications/mixed/prompts/added.md", "content": "NEW RESOURCE"},
                {"path": "applications/mixed/prompts/obsolete.md", "content": None},
            ])
        if scenario in {"unsafe_tools", "registry_tools"} and role in {"general", "domain", "compose"}:
            import yaml

            worker = yaml.safe_load((app / "workflows/worker.yaml").read_text())
            if scenario == "unsafe_tools":
                module, function = {
                    "general": ("agentloom.config.private_bindings", "resolve_bound_credentials"),
                    "domain": ("builtins", "eval"),
                    "compose": ("agentloom.config.config", "current_harness_model_config"),
                }[role]
                worker["tools"] = [{"name": "expose_private", "module": module, "function": function}]
            elif role == "compose":
                worker["tools"] = [{"name": "read_file"}]
            result["edits"] = [{"path": "applications/mixed/workflows/worker.yaml", "content": yaml.safe_dump(worker)}]
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
            (app / "prompts/large.md").write_text("x" * (9 * 1024 * 1024) + "END-OF-COMPLETE-EVIDENCE")
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
                value.update(agent_runtime=runtime, system_prompt=(resources / f"{role}.md").read_text(), tools=[])
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
            request, policy=policy, state_root=tmp_path / "experiments", environment=environment, stop_token=stop,
            experiment_id="preallocated-experiment",
            events=(lambda event: stop.set() if event["event"] == "trial.completed"
                    and event.get("trial_id") == "final1" else None) if scenario == "stop_after_final" else None,
        )
        assert result.experiment_id == "preallocated-experiment"
        if scenario == "stop_after_final":
            assert result.status == "stopped", result.to_dict()
            assert len(result.trials) == 4
            assert not result.delivery and not (tmp_path / "delivery.json").exists()
            assert requests_by_role.count("reflect") == requests_by_role.count("select") == 0
            assert not (tmp_path / "experiments" / result.experiment_id / "stages/publication.json").exists()
            business_calls = requests_by_role.count("business")
            assert business_calls == 4
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
        assert result.output == {
            "fact": "FACT-123",
            "variant": "BASE" if scenario in {"invalid_edits", "duplicate_edits", "unsafe_tools", "registry_tools"} else "COMPOSED",
        }
        assert all(t.acceptance["hidden"] == "denied" for t in result.trials)
        assert requests_by_role.count("business") == 4 * k + (scenario == "stop_and_resume")
        edits = 2 * k if scenario in {"invalid_edits", "duplicate_edits", "unsafe_tools"} else k
        assert requests_by_role.count("general") == requests_by_role.count("domain") == edits
        assert requests_by_role.count("compose") == edits
        assert requests_by_role.count("select") == (k > 1)
        assert requests_by_role.count("reflect") == 1
        if not custom_roles:
            assert sum(item["model"] == "improvement" for item in requests) == sum(
                role != "business" for role in requests_by_role) + len(evidence_reads)
        # Built-in roles now use Pi even when business execution uses smol.
        # This fixture's Pi adapter does not report every usage dimension.
        assert result.usage["complete"] is False
        assert result.usage["total_tokens"] == 70 * len(requests)
        for role_receipt in result.roles:
            observed = role_receipt["receipt"]["usage_observations"]
            assert observed and all(item["attempt_id"] for item in observed)
        state = tmp_path / "experiments" / result.experiment_id
        if scenario == "large_evidence":
            import hashlib

            assert evidence_reads == ["index", "retrieve-index", "tail"]
            assert all(role["status"] == "completed" for role in result.roles)
            evidence_root = state / "roles/general1.edit1/workspace/harness-evidence"
            index = json.loads((evidence_root / "index.json").read_text())
            value = index["files"]["applications/mixed/prompts/large.md"]
            parts = value["harness_text_parts"]
            restored = "".join(Path(parts["path_template"].format(index=i)).read_text()
                               for i in range(parts["count"]))
            assert restored == (app / "prompts/large.md").read_text()
            assert hashlib.sha256(restored.encode()).hexdigest() == value["sha256"]
            assert all(len(json.dumps(item).encode()) < 512 * 1024 for item in requests)
        assert (state / "stages/publication.json").exists()
        if scenario == "normal" and mode == "parallel":
            candidate = json.loads((state / "stages/candidate.final1.json").read_text())["result"]
            prompts = Path(candidate["root"]) / "project/applications/mixed/prompts"
            assert (prompts / "business.md").read_text() == "COMPOSED"
            assert (prompts / "added.md").read_text() == "NEW RESOURCE"
            assert not (prompts / "obsolete.md").exists()
        if scenario == "duplicate_edits":
            for label in ("general1", "domain1", "final1"):
                candidate = json.loads((state / f"stages/candidate.{label}.json").read_text())["result"]
                assert candidate["inherited"]
                assert "each path only once" in candidate["error"]
        if scenario == "unsafe_tools":
            for label in ("general1", "domain1", "final1"):
                candidate = json.loads((state / f"stages/candidate.{label}.json").read_text())["result"]
                assert candidate["inherited"]
                assert "unapproved Python tool binding" in candidate["error"]
            assert all("fixture-key" not in json.dumps(item) for item in requests)
        if scenario == "development_score":
            evaluations = list((state / "stages").glob("evaluation.*.json"))
            assert len(evaluations) == 4 * k
            assert all(json.loads(path.read_text())["result"]["partition"] == "development-a" for path in evaluations)
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
