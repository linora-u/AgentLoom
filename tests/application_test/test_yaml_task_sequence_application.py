"""Configured user turns exercised through the public Application run."""

import json

import pytest
import yaml
from agentloom.app.run import ApplicationRunError
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.checkpoint import CheckpointManager
from agentloom.execution.observability import inspect_run

from tests.application_test.mixed_runtime_support import finish, model_service, project, tool_messages, write_yaml


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_yaml_task_list_continues_one_agent_conversation(tmp_path, runtime):
    def program(request):
        messages = request["messages"]
        visible = "\n".join(str(message.get("content")) for message in messages)
        if "SECOND-TURN-731" in visible:
            assert "FIRST-TURN-731" in visible
            assert "ANSWER-A-731" in visible
            return finish(request, "ANSWER-B-731")
        assert "FIRST-TURN-731" in visible
        return finish(request, "ANSWER-A-731")

    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
        agent.pop("workflow", None)
        agent["worker_agents"] = []
        agent["task"] = ["FIRST-TURN-731", "SECOND-TURN-731"]
        write_yaml(definition, agent)

        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)

    assert result.output == "ANSWER-B-731"
    assert len(requests) >= 2
    trace_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((result.run.trace_dir / "events" / result.run.run_id).glob("*.json"))
    ]
    assert sorted({
        event["run_step_number"]
        for event in trace_events if event["kind"] == "model_request"
    }) == [1, 2]
    with inspect_run(result.run) as trace:
        items = [event for event in trace.events() if event["kind"] == "task_item_end"]
        assert [(item["item_index"], trace.read_text(item["answer_ref"])) for item in items] == [
            (0, "ANSWER-A-731"), (1, "ANSWER-B-731"),
        ]


def test_system_prompt_path_is_loaded_into_agent_instructions(tmp_path):
    def program(request):
        system_text = "\n".join(str(message.get("content")) for message in request["messages"] if message["role"] == "system")
        assert "SYSTEM-PROMPT-829" in system_text
        return finish(request, "PATH-OK-829")

    with model_service(program) as (url, _requests):
        definition = project(tmp_path, url, supervisor="smolagents", worker="smolagents")
        agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
        agent.pop("workflow", None)
        agent["worker_agents"] = []
        agent["task"] = "Check the configured system prompt."
        agent["system_prompt"] = {"path": "../config/prompts/instructions.md"}
        write_yaml(definition, agent)
        prompt = definition.parent.parent / "config/prompts/instructions.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("SYSTEM-PROMPT-829", encoding="utf-8")

        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)

    assert result.output == "PATH-OK-829"


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_worker_uses_yaml_task_and_schema_data_without_runtime_task(tmp_path, runtime):
    def program(request):
        if request["model"] == "worker":
            visible = "\n".join(str(message.get("content")) for message in request["messages"])
            assert "WORKER-TASK-994" in visible
            assert "REPORT-PATH-994" in visible
            return finish(request, "WORKER-RESULT-994")
        if not tool_messages(request):
            return [("call-worker-994", "inspect_note", {"file_path": "REPORT-PATH-994"})]
        return finish(request, "ROOT-RESULT-994")

    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        root = yaml.safe_load(definition.read_text(encoding="utf-8"))
        root.pop("workflow", None)
        root["task"] = "Ask inspect_note to analyze its file_path."
        write_yaml(definition, root)
        worker_path = definition.parent / "worker_agents/inspect.yaml"
        worker = yaml.safe_load(worker_path.read_text(encoding="utf-8"))
        worker.pop("workflow", None)
        worker["task"] = "WORKER-TASK-994: analyze the supplied file path."
        worker["input_schema"] = {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
            "additionalProperties": False,
        }
        write_yaml(worker_path, worker)

        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)

    assert result.output == "ROOT-RESULT-994"
    assert any(request["model"] == "worker" for request in requests)


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_worker_yaml_task_list_shares_its_tool_invocation_conversation(tmp_path, runtime):
    def program(request):
        if request["model"] == "worker":
            visible = "\n".join(str(message.get("content")) for message in request["messages"])
            if "WORKER-SECOND-441" in visible:
                assert "WORKER-FIRST-441" in visible
                assert "WORKER-ANSWER-A-441" in visible
                return finish(request, "WORKER-ANSWER-B-441")
            assert "WORKER-FIRST-441" in visible
            return finish(request, "WORKER-ANSWER-A-441")
        if not tool_messages(request):
            return [("call-worker-441", "inspect_note", {"query": "note.txt"})]
        assert "WORKER-ANSWER-B-441" in tool_messages(request)[-1]["content"]
        return finish(request, "ROOT-ANSWER-441")

    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        worker_path = definition.parent / "worker_agents/inspect.yaml"
        worker = yaml.safe_load(worker_path.read_text(encoding="utf-8"))
        worker["tools"] = []
        worker["task"] = ["WORKER-FIRST-441", "WORKER-SECOND-441"]
        write_yaml(worker_path, worker)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)

    assert result.output == "ROOT-ANSWER-441"
    assert len([request for request in requests if request["model"] == "worker"]) >= 2
    with inspect_run(result.run) as trace:
        assert [trace.read_text(event["answer_ref"]) for event in trace.events()
                if event["kind"] == "task_item_end" and event["item_count"] == 2] == [
            "WORKER-ANSWER-A-441", "WORKER-ANSWER-B-441",
        ]


def test_application_execution_has_no_task_override_argument():
    with pytest.raises(TypeError, match="task_override"):
        execute_app("unused.yaml", task_override="An unconfigured task")


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_goal_list_completes_each_yaml_task_in_one_conversation(tmp_path, runtime, monkeypatch):
    def program(request):
        visible = "\n".join(str(message.get("content")) for message in request["messages"])
        completed = visible.count('"status":"complete"') + visible.count("'status': 'complete'")
        if "GOAL-SECOND-284" in visible:
            assert "GOAL-FIRST-284" in visible
            if completed < 2:
                return [("complete-second-284", "update_goal", {
                    "status": "complete", "evidence": "Second phase verified",
                })]
            return finish(request, "SECOND-ANSWER-284")
        assert "GOAL-FIRST-284" in visible
        if completed == 0:
            return [("complete-first-284", "update_goal", {
                "status": "complete", "evidence": "First phase verified",
            })]
        return finish(request, "FIRST-ANSWER-284")

    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
        agent["worker_agents"] = []
        agent["task"] = ["GOAL-FIRST-284", "GOAL-SECOND-284"]
        agent["goal"] = True
        write_yaml(definition, agent)
        system = tmp_path / "config/system.yaml"
        system_config = yaml.safe_load(system.read_text(encoding="utf-8"))
        system_config["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
        write_yaml(system, system_config)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)

    assert result.goal is not None
    assert result.goal["status"] == "complete"
    assert result.goal["phase_index"] == 1
    assert any("GOAL-SECOND-284" in str(request["messages"]) for request in requests)
    checkpoint_path = tmp_path / "runtime/checkpoints/mixed" / result.run.task_id / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["task_item_next_index"] == 2
    assert checkpoint["goal_phase_commit"]["goal_id"] == result.goal["goal_id"]
    with inspect_run(result.run) as trace:
        assert [event["item_index"] for event in trace.events()
                if event["kind"] == "task_item_end"] == [0, 1]

    # Model a crash tail where Goal completion survived but the matching
    # Runtime boundary did not. Resume must stop before another model call.
    checkpoint["task_item_next_index"] = 1
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    manager = CheckpointManager("mixed", checkpoint_dir=checkpoint_path.parent)
    manager.record_task_status_changed(result.run.task_id, "failed", error="injected crash")
    with bind_config(load_project_config(tmp_path)):
        with pytest.raises(ApplicationRunError, match="Cannot safely resume Goal"):
            execute_app(definition, file_logging=False, resume_task_id=result.run.task_id)

    def explicit_null_program(request):
        names = {tool["function"]["name"] for tool in request.get("tools", [])}
        if "final_answer" in names:
            return [("explicit-null-284", "final_answer", {"answer": None})]
        return "null"

    with model_service(explicit_null_program) as (url, _requests):
        root = tmp_path / f"explicit_null_{runtime}"
        definition = project(root, url, supervisor=runtime, worker=runtime)
        agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
        agent["worker_agents"] = []
        agent["task"] = "RETURN-EXPLICIT-NULL-284"
        agent["output_schema"] = {"type": "null"}
        write_yaml(definition, agent)
        system = root / "config/system.yaml"
        system_config = yaml.safe_load(system.read_text(encoding="utf-8"))
        system_config["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
        write_yaml(system, system_config)
        explicit_events = []
        with bind_config(load_project_config(root)):
            explicit_null = execute_app(
                definition, file_logging=False, event_sink=explicit_events.append,
            )
        assert explicit_null.output is None
        assert explicit_null.answer_present is True
        assert next(event for event in explicit_events if event.event == "run.completed").answer_present is True
        with inspect_run(explicit_null.run) as trace:
            events = trace.events()
            items = [event for event in events if event["kind"] == "task_item_end"]
            assert len(items) == 1
            assert trace.read_text(items[0]["answer_ref"]) == "null"
            assert [trace.read_text(event["answer_ref"]) for event in events
                    if event["kind"] == "final_answer"] == ["null"]
        checkpoint_path = root / "runtime/checkpoints/mixed" / explicit_null.run.task_id / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["task_item_answer_present"] is True
        manager = CheckpointManager("mixed", checkpoint_dir=checkpoint_path.parent)
        manager.record_task_status_changed(explicit_null.run.task_id, "failed", error="injected tail")
        with bind_config(load_project_config(root)):
            resumed_null = execute_app(
                definition, file_logging=False, resume_task_id=explicit_null.run.task_id,
            )
        assert resumed_null.output is None
        assert resumed_null.answer_present is True

    if runtime == "smolagents":
        def complete_without_reply(_request):
            return [("complete-null-284", "update_goal", {
                "status": "complete", "evidence": "Verified without a final reply",
            })]

        with model_service(complete_without_reply) as (url, _requests):
            for requires_reply in (False, True):
                root = tmp_path / ("schema_null" if requires_reply else "plain_null")
                definition = project(root, url, supervisor=runtime, worker=runtime)
                agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
                agent["worker_agents"] = []
                agent["task"] = "GOAL-WITHOUT-REPLY-284"
                agent["goal"] = True
                agent["runtime_options"] = {"max_steps": 1, "smart_summary": False}
                if requires_reply:
                    agent["output_schema"] = {"type": "string"}
                write_yaml(definition, agent)
                with bind_config(load_project_config(root)):
                    if requires_reply:
                        with pytest.raises(ApplicationRunError, match="output_schema"):
                            execute_app(definition, file_logging=False)
                    else:
                        no_reply_events = []
                        no_reply = execute_app(
                            definition, file_logging=False, event_sink=no_reply_events.append,
                        )
                        assert no_reply.output is None
                        assert no_reply.answer_present is False
                        assert next(event for event in no_reply_events
                                    if event.event == "run.completed").answer_present is False
                        with inspect_run(no_reply.run) as trace:
                            events = trace.events()
                            items = [event for event in events if event["kind"] == "task_item_end"]
                            assert len(items) == 1
                            assert trace.read_text(items[0]["output_ref"]) == "null"
                            assert items[0]["answer_ref"] is None
                            assert not any(event["kind"] == "final_answer" for event in events)

        from agentloom.execution.checkpoint.coordinator import CheckpointCoordinator
        from agentloom.execution.goal.provider import GoalStateProvider
        from agentloom.execution.observability import TraceRecorder

        def complete_phase(request):
            visible = "\n".join(str(message.get("content")) for message in request["messages"])
            phase = "second" if "FAULT-SECOND-284" in visible else "first"
            return [(f"complete-{phase}-284", "update_goal", {
                "status": "complete", "evidence": f"{phase} phase verified",
            })]

        for fault in ("checkpoint", "advance", "trace"):
            with model_service(complete_phase) as (url, requests):
                root = tmp_path / f"fault_{fault}"
                definition = project(root, url, supervisor=runtime, worker=runtime)
                agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
                agent["worker_agents"] = []
                agent["task"] = ["FAULT-FIRST-284", "FAULT-SECOND-284"]
                agent["goal"] = True
                agent["runtime_options"] = {"max_steps": 1, "smart_summary": False}
                write_yaml(definition, agent)
                system = root / "config/system.yaml"
                system_config = yaml.safe_load(system.read_text(encoding="utf-8"))
                system_config["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
                write_yaml(system, system_config)

                observed = []
                with monkeypatch.context() as patch:
                    if fault == "checkpoint":
                        original = CheckpointCoordinator.save_runtime_checkpoint

                        def fail_checkpoint(coordinator, *args, _original=original, **kwargs):
                            if kwargs.get("task_item_next_index") == 1:
                                raise OSError("injected Goal checkpoint interruption")
                            return _original(coordinator, *args, **kwargs)

                        patch.setattr(CheckpointCoordinator, "save_runtime_checkpoint", fail_checkpoint)
                    elif fault == "advance":
                        def fail_advance(*_args, **_kwargs):
                            raise OSError("injected Goal phase advance interruption")

                        patch.setattr(GoalStateProvider, "advance_to", fail_advance)
                    else:
                        original = TraceRecorder._append

                        def fail_trace(recorder, event, _original=original):
                            if event["kind"] == "task_item_end" and event["item_index"] == 0:
                                raise OSError("injected Goal trace interruption")
                            return _original(recorder, event)

                        patch.setattr(TraceRecorder, "_append", fail_trace)

                    with bind_config(load_project_config(root)):
                        with pytest.raises(ApplicationRunError, match="injected Goal"):
                            execute_app(definition, file_logging=False, event_sink=observed.append)

                task_ids = {event.run.task_id for event in observed if event.run is not None}
                assert len(task_ids) == 1
                task_id = task_ids.pop()
                assert not any("FAULT-SECOND-284" in str(request["messages"]) for request in requests)
                with bind_config(load_project_config(root)):
                    if fault == "checkpoint":
                        with pytest.raises(ApplicationRunError, match="Cannot safely resume Goal"):
                            execute_app(definition, file_logging=False, resume_task_id=task_id)
                    else:
                        resumed = execute_app(definition, file_logging=False, resume_task_id=task_id)
                        assert resumed.output is None
                        assert any("FAULT-SECOND-284" in str(request["messages"]) for request in requests)


@pytest.mark.parametrize("runtime", ["pi", "smolagents"])
def test_resume_reconciles_committed_yaml_task_item_without_replaying_it(
    tmp_path, runtime, monkeypatch,
):
    from agentloom.execution.observability import TraceRecorder

    def program(request):
        visible = "\n".join(str(message.get("content")) for message in request["messages"])
        if "SECOND-RESUME-518" in visible:
            assert "FIRST-ANSWER-518" in visible
            return finish(request, "SECOND-ANSWER-518")
        return finish(request, "FIRST-ANSWER-518")

    original_append = TraceRecorder._append
    failed_once = False

    def fail_first_item_trace(recorder, event):
        nonlocal failed_once
        if event["kind"] == "task_item_end" and event["item_index"] == 0 and not failed_once:
            failed_once = True
            raise OSError("fixture item trace interruption")
        return original_append(recorder, event)

    monkeypatch.setattr(TraceRecorder, "_append", fail_first_item_trace)
    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        agent = yaml.safe_load(definition.read_text(encoding="utf-8"))
        agent["worker_agents"] = []
        agent["task"] = ["FIRST-RESUME-518", "SECOND-RESUME-518"]
        write_yaml(definition, agent)
        system = tmp_path / "config/system.yaml"
        system_config = yaml.safe_load(system.read_text(encoding="utf-8"))
        system_config["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
        write_yaml(system, system_config)
        observed = []
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError, match="fixture item trace interruption"):
                execute_app(definition, file_logging=False, event_sink=observed.append)
            task_ids = {event.run.task_id for event in observed if event.run is not None}
            assert len(task_ids) == 1
            task_id = task_ids.pop()
            checkpoint_path = tmp_path / "runtime/checkpoints/mixed" / task_id / "checkpoint.json"
            committed_checkpoint = checkpoint_path.read_text(encoding="utf-8")
            damaged = json.loads(committed_checkpoint)
            assert damaged["task_item_next_index"] == 1
            damaged.pop("runtime_checkpoint")
            checkpoint_path.write_text(json.dumps(damaged), encoding="utf-8")
            request_count = len(requests)
            with pytest.raises(ApplicationRunError, match="no Runtime checkpoint"):
                execute_app(definition, file_logging=False, resume_task_id=task_id)
            assert len(requests) == request_count
            checkpoint_path.write_text(committed_checkpoint, encoding="utf-8")
            resumed = execute_app(
                definition, file_logging=False, resume_task_id=task_id,
            )

    assert resumed.output == "SECOND-ANSWER-518"
    first_requests = [request for request in requests if "FIRST-RESUME-518" in str(request["messages"])
                      and "SECOND-RESUME-518" not in str(request["messages"])]
    assert len(first_requests) == 1
    with inspect_run(resumed.run) as trace:
        items = [event for event in trace.events() if event["kind"] == "task_item_end"]
        assert [event["item_index"] for event in items] == [0, 1]
        assert all(event["state"] == "committed" and event["commit_id"] for event in items)
        assert items[0]["commit_id"] != items[1]["commit_id"]


def test_resumed_worker_task_list_keeps_first_item_context(tmp_path):
    def program(request):
        messages = tool_messages(request)
        if request["model"] == "worker":
            visible = "\n".join(str(message.get("content")) for message in request["messages"])
            if "WORKER-SECOND-639" in visible:
                assert "WORKER-ANSWER-A-639" in visible
                return finish(request, "WORKER-ANSWER-B-639")
            return finish(request, "WORKER-ANSWER-A-639")
        if not messages:
            return [("worker-call-639", "inspect_note", {"query": "note.txt"})]
        if "WORKER-ANSWER-B-639" in str(messages[-1]["content"]):
            return finish(request, "ROOT-ANSWER-639")
        return [("worker-retry-639", "inspect_note", {"query": "note.txt"})]

    with model_service(program, fail_requests={3, 4}) as (url, requests):
        definition = project(tmp_path, url, supervisor="pi", worker="pi")
        worker_path = definition.parent / "worker_agents/inspect.yaml"
        worker = yaml.safe_load(worker_path.read_text(encoding="utf-8"))
        worker["tools"] = []
        worker["task"] = ["WORKER-FIRST-639", "WORKER-SECOND-639"]
        write_yaml(worker_path, worker)
        system = tmp_path / "config/system.yaml"
        system_config = yaml.safe_load(system.read_text(encoding="utf-8"))
        system_config["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
        write_yaml(system, system_config)
        observed = []
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError):
                execute_app(definition, file_logging=False, event_sink=observed.append)
            task_ids = {event.run.task_id for event in observed if event.run is not None}
            assert len(task_ids) == 1
            resumed = execute_app(
                definition, file_logging=False, resume_task_id=task_ids.pop(),
            )

    assert resumed.output == "ROOT-ANSWER-639"
    first_item_requests = [request for request in requests
                           if request["model"] == "worker"
                           and "WORKER-FIRST-639" in str(request["messages"])
                           and "WORKER-SECOND-639" not in str(request["messages"])]
    assert len(first_item_requests) == 1
