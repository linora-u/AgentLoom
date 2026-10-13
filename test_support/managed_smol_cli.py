"""Formal smol CLI fixture; only the ModelTurnBinding uses a local provider."""
import json
import os
from pathlib import Path
import shlex
import sys
import time

import psutil
import yaml


def main():
    from agentloom.__main__ import main as loom_main
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.execution import get_current_run_context
    from agentloom.execution.model_binding import ModelTurnBinding
    from agentloom.execution.model_protocol import FunctionCallItem, ModelTurnResult, ModelUsage
    from agentloom.execution.process_scope import bind_managed_process_scope
    from agentloom.integrations.litellm import model_binding
    from agentloom.app import runner
    from agentloom.runtimes.smolagents.tools.shell.background_task import BackgroundTaskRegistry

    root, mode = Path(sys.argv[1]), sys.argv[2]
    config = root / "config"
    config.mkdir()
    (config / "system.yaml").write_text(yaml.safe_dump({
        "runtime": {"root_dir": str(root / "runtime")},
        "checkpoint": {"enabled": False}, "default_toolsets": [], "skills": {"paths": []},
        "logging": {"console_enabled": False, "file_enabled": False}, "self_learning": {"enabled": False},
        "shell_settings": {"allowed_commands": ["*"], "allowed_operators": ["*"],
            "background_tasks": {"enabled": mode != "timeout", "auto_background_on_timeout": mode != "timeout", "stall_detection": False}},
    }))
    (config / "model.yaml").write_text(yaml.safe_dump({"model": {
        "default_model_type": "local", "local": {"model": "scope-smol-model", "adapter": "openai_chat"},
    }}))
    app = root / "applications/smol/workflows/supervisor.yaml"
    app.parent.mkdir(parents=True)
    app.write_text(yaml.safe_dump({"name": "scope_smol", "agent_runtime": "smolagents",
        "description": "Verify managed shell lifecycle through the formal CLI.",
        "model_type": "local", "task": "Run the requested shell command and finish.",
        "runtime_options": {"max_steps": 4, "smart_summary": False, "todo_mode": "off"},
        "toolsets": [], "tools": [{"name": "shell_tool"}],
    }))
    marker = root / "native.json"
    code = "import os,json,psutil,time;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':os.getpid(),'creation':psutil.Process().create_time(),'group':os.getpgrp()}))"
    if mode != "foreground":
        code += ";time.sleep(60)"
    command = shlex.join([sys.executable, "-c", code])
    evidence = {"root_pid": os.getpid(), "root_group": os.getpgrp(), "models_requested": 0, "mode": mode}

    # Observe the public runner exception before the CLI renders its concise
    # summary. Do not make arbitrary exception text part of the CLI protocol.
    original_execute = runner.execute_app

    def error_causes(error):
        children = list(getattr(error, "exceptions", ()))
        if error.__cause__ is not None:
            children.append(error.__cause__)
        original = getattr(error, "original_error", None)
        if original is not None and original not in children:
            children.append(original)
        return [str(error)] + [message for child in children for message in error_causes(child)]

    def execute(*args, **kwargs):
        try:
            return original_execute(*args, **kwargs)
        except BaseException as error:
            evidence["runner_error_causes"] = error_causes(error)
            raise

    runner.execute_app = execute

    if mode == "cleanup-failure":
        original_register = BackgroundTaskRegistry.register

        def register(registry, *args, **kwargs):
            original_close = kwargs["close_process"]
            failed = False

            def close():
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected-background-close-failure")
                original_close()

            kwargs["close_process"] = close
            return original_register(registry, *args, **kwargs)

        BackgroundTaskRegistry.register = register

    class Provider:
        adapter_id = "openai_chat"
        count = 0

        def turn(self, request):
            self.count += 1
            if self.count == 1:
                name, arguments = "shell_tool", {"command": command, "load_profile": False, "timeout": 1,
                    "run_in_background": mode in {"background", "cleanup-failure"}}
            else:
                assert self.count == 2
                # A background launch returns before its command reaches our
                # evidence barrier. Wait for that barrier, not a guessed sleep.
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert marker.exists(), "the real shell never executed"
                evidence["native"] = json.loads(marker.read_text())
                tasks = BackgroundTaskRegistry.get_instance().list_running()
                evidence["background_tasks"] = [task.task_id for task in tasks]
                evidence["native_alive_before_final"] = psutil.pid_exists(evidence["native"]["pid"])
                evidence["run_id"] = get_current_run_context().run_id
                name, arguments = "final_answer", {"answer": "smol-scope-complete"}
            return ModelTurnResult(items=(FunctionCallItem(call_id=f"scope-{self.count}", name=name,
                arguments_json=json.dumps(arguments)),), usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15))

    provider = Provider()
    binding = ModelTurnBinding(model_type="local", model_id="scope-smol-model", adapter=provider,
        max_tokens=4096, context_window=32768, max_output_tokens=4096, input_token_limit=28672, requests_per_minute=60)
    model_binding.resolve_litellm_model_turn_binding = lambda *_args, **_kwargs: binding
    try:
        with bind_managed_process_scope(), bind_config(load_project_config(root)):
            loom_main(["run", str(app), "--no-file-log", "--output-format", "jsonl"])
    finally:
        if marker.exists():
            native = json.loads(marker.read_text())
            evidence["native_alive_after_cli"] = psutil.pid_exists(native["pid"])
        (root / "evidence.json").write_text(json.dumps(evidence))


if __name__ == "__main__":
    main()
