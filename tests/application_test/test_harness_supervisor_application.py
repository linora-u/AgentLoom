"""The existing complete Supervisor entry dispatches only enabled applications."""
import json

import pytest
import yaml

from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from tests.application_test.mixed_runtime_support import finish, model_service, project, tool_messages, write_yaml


@pytest.mark.parametrize("runtime", ["smolagents", "pi"])
def test_enabled_supervisor_executes_and_adopts_final_with_workers_and_recovers(tmp_path, runtime):
    from agentloom.runtimes.pi.credentials import bind_pi_credentials

    counts = []

    def program(request):
        assert "MCP-ARGV-PRIVATE-CANARY" not in json.dumps(request)
        assert "DOTENV-PRIVATE-CANARY" not in json.dumps(request)
        system = str(request["messages"][0]["content"])
        if "Extract only supported reusable experiences" in system:
            value, role = {"experiences": []}, "reflect"
        elif "Compose one coherent complete Application" in system:
            value, role = {"edits": {"applications/mixed/prompts/root.md": "COMPOSED-ROOT"},
                           "rationale": "Use the independent evidence."}, "compose"
        elif "domain-specific working methods" in system:
            value, role = {"edits": {}, "rationale": "No further changes."}, "domain"
        elif "general problem-solving" in system:
            value, role = {"edits": {}, "rationale": "No further changes."}, "general"
        elif request["model"] == "worker":
            counts.append("worker")
            return finish(request, "INDEPENDENT-EVIDENCE")
        else:
            if not tool_messages(request):
                return [("consult", "inspect_note", {"query": "Provide the independent evidence."})]
            assert "INDEPENDENT-EVIDENCE" in tool_messages(request)[-1]["content"]
            counts.append("business")
            return finish(request, "COMPOSED-RESULT" if "COMPOSED-ROOT" in system else "BASE-RESULT")
        counts.append(role)
        return finish(request, value)

    with model_service(program) as (url, requests):
        definition = project(tmp_path, url, supervisor=runtime, worker=runtime)
        app = definition.parents[1]
        root = yaml.safe_load(definition.read_text())
        root["system_prompt"] = {"path": "prompts/root.md"}
        write_yaml(definition, root)
        (app / "prompts").mkdir()
        (app / "prompts/root.md").write_text("BASE-ROOT")
        for name in (".env.local", ".env.production"):
            (app / name).write_text("OPENAI_API_KEY=DOTENV-PRIVATE-CANARY\n")
        (app / ".mcp.json").write_text(json.dumps({"mcpServers": {"private": {
            "command": "/usr/bin/env", "args": ["--token", "MCP-ARGV-PRIVATE-CANARY"]}}}))
        worker = definition.parent / "worker_agents/inspect.yaml"
        value = yaml.safe_load(worker.read_text())
        value["tools"] = []
        write_yaml(worker, value)
        write_yaml(app / "config/system.yaml", {"harnessing": {"enabled": True}})
        events = []
        private = {"unused-provider": {"type": "oauth", "access": "PRIVATE-OAUTH-CANARY-136",
                                      "refresh": "PRIVATE-REFRESH-CANARY-136", "expires": 9999999999999}}
        with bind_config(load_project_config(tmp_path)), bind_pi_credentials(private):
            result = execute_app(definition, file_logging=False, event_sink=events.append)
            assert result.output == "COMPOSED-RESULT"
            assert result.harnessing.status == "completed", result.harnessing.to_dict()
            assert result.harnessing.selected_trial_id.startswith("final")
            selected = next(trial for trial in result.harnessing.trials
                            if trial.trial_id == result.harnessing.selected_trial_id)
            assert result.run.run_id == selected.runs[-1]["run_id"]
            assert counts.count("business") == counts.count("worker") == 4
            assert result.harnessing.usage["total_tokens"] == len(requests) * 70
            assert len(events) == 8
            assert len({event.run.run_dir for event in events}) == 4
            calls = len(requests)
            # Recovery must not parse mutable source definitions or reload
            # Worker/prompt resources, even through the public CLI entry.
            definition.write_text("invalid: [")
            worker.write_text("invalid: [")
            (app / "config/system.yaml").write_text("invalid: [")
            (app / "prompts/root.md").write_text("LIVE-MUTATION")
            recovered = execute_app(definition, file_logging=False,
                harness_resume_id=result.harnessing.experiment_id)
            assert recovered.output == result.output
            assert recovered.run == result.run
            assert len(requests) == calls
            definition.unlink()
            from click.testing import CliRunner
            from agentloom.app.cli import run
            command = CliRunner().invoke(run, [str(definition), "--no-file-log",
                "--resume-experiment", result.harnessing.experiment_id, "--output-format", "json"])
            assert command.exit_code == 0, command.output
            message = json.loads(command.stdout)
            assert message["event"] == "run.completed"
            assert message["harnessing"]["selected_trial_id"] == result.harnessing.selected_trial_id
            assert message["run"]["run_id"] == result.run.run_id
            assert len(requests) == calls
            for path in (tmp_path / "runtime").rglob("*"):
                if path.is_file():
                    assert b"PRIVATE-OAUTH-CANARY-136" not in path.read_bytes()
                    assert b"PRIVATE-REFRESH-CANARY-136" not in path.read_bytes()
                    assert b"MCP-ARGV-PRIVATE-CANARY" not in path.read_bytes()
                    assert b"DOTENV-PRIVATE-CANARY" not in path.read_bytes()
            with pytest.raises(ValueError, match="harness_resume_id"):
                write_yaml(app / "config/system.yaml", {"harnessing": {"enabled": True}})
                write_yaml(definition, root)
                execute_app(definition, resume_task_id=result.run.task_id)


@pytest.mark.parametrize("output", [None, "123", {"x": [1, None, "text"]}])
def test_complete_runner_persists_typed_result_after_resource_cleanup(tmp_path, monkeypatch, output):
    from agentloom.app.factory import YamlConfiguredSupervisorAgent

    with model_service(lambda request: finish(request, "unused")) as (url, _):
        definition = project(tmp_path, url, supervisor="smolagents", worker="smolagents")
        monkeypatch.setattr(YamlConfiguredSupervisorAgent, "run", lambda *args, **kwargs: output)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(definition, file_logging=False)
    manifest = json.loads(result.run.manifest_path.read_text())
    assert manifest["cleanup_complete"] is True
    artifact = result.run.run_dir / manifest["result_json_artifact"]
    assert json.loads(artifact.read_text()) == output
    assert isinstance(json.loads(artifact.read_text()), type(output))
