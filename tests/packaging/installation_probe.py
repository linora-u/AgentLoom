"""Installed-package acceptance probe; run with the environment's own Python.

Every child runs outside the checkout. Only model generation is deterministic;
the generated scaffold, Application runner, tool execution and Run storage are real.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

MODEL_RUNNER = r'''
import importlib.abc
import importlib.machinery
import json
from pathlib import Path
import runpy
import sys
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    ModelTurnResult,
    ModelUsage,
)

class ModelTurnAdapter:
    adapter_id = 'openai_chat'
    def __init__(self):
        self.calls = 0
        self.requests = []
    def turn(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            name, arguments = 'write_probe', {'value': 'external-tool-ok'}
        else:
            assert any(
                isinstance(item, FunctionCallOutputItem)
                and 'external-tool-ok:helper' in item.output
                for item in request.items
            ), request.items
            name, arguments = 'final_answer', {'answer': 'installation-complete'}
        assert {tool.name for tool in request.tools} >= {'write_probe', 'final_answer'}
        return ModelTurnResult(
            items=(
                FunctionCallItem(
                    call_id=f'probe-{self.calls}',
                    name=name,
                    arguments_json=json.dumps(arguments),
                ),
            ),
            usage=ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5),
        )
adapter = ModelTurnAdapter()
binding = ModelTurnBinding(
    model_type='probe',
    model_id='deterministic-installation-probe',
    adapter=adapter,
    max_tokens=4096,
    context_window=32768,
    max_output_tokens=4096,
    input_token_limit=28672,
    requests_per_minute=60,
)

# Intercept only the canonical binding resolver. The generated program itself
# establishes project context before importing the real Application runner.
class Loader(importlib.abc.Loader):
    def __init__(self, wrapped): self.wrapped = wrapped
    def create_module(self, spec): return None
    def exec_module(self, module):
        self.wrapped.exec_module(module)
        module.resolve_litellm_model_turn_binding = (
            lambda *args, **kwargs: binding
        )
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agentloom.adapters.litellm.model_binding':
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            spec.loader = Loader(spec.loader)
            return spec
sys.meta_path.insert(0, Finder())
runpy.run_path(sys.argv[1], run_name='__main__')
assert adapter.calls == 2, adapter.calls
assert len(adapter.requests) == 2
assert Path(sys.argv[2]).read_text() == 'external-tool-ok:helper'
from agentloom.configuration import C
assert Path(C.agent_root) == Path(sys.argv[3])
print('GENERATED_APPLICATION_PASS')
'''


def probe(workspace: Path) -> dict:
    workspace.mkdir(parents=True, exist_ok=False)
    project = workspace / "project with spaces"
    outside = workspace / "unrelated cwd"
    outside.mkdir()
    config = project / "config"
    config.mkdir(parents=True)
    runtime = workspace / "runtime"
    (config / "system.yaml").write_text(textwrap.dedent(f"""\
        runtime:
          root_dir: {json.dumps(str(runtime))}
        logging:
          console_enabled: false
          file_enabled: true
        checkpoint:
          enabled: false
        self_learning:
          enabled: false
        lsp_servers:
          enabled: false
        default_toolsets: []
        todo:
          mode: off
    """))
    (config / "llm.yaml").write_text(
        "model:\n"
        "  default_model_type: probe\n"
        "  probe:\n"
        "    model: openai/installation-probe\n"
        "    adapter: openai_chat\n"
        "    api_key: synthetic-not-used\n"
        "  summary:\n"
        "    model: openai/installation-summary\n"
        "    adapter: openai_chat\n"
    )
    app = project / "applications" / "nested" / "probe"
    (app / "workflows").mkdir(parents=True)
    (app / "helper.py").write_text("SUFFIX = ':helper'\n")
    (app / "tools.py").write_text(textwrap.dedent('''\
        from pathlib import Path
        from agentloom.configuration import C
        from .helper import SUFFIX

        def write_probe(value: str) -> str:
            """Write the installation probe artifact.

            Args:
                value: Verified probe value.
            """
            result = value + SUFFIX
            (Path(C.agent_root) / 'probe-artifact.txt').write_text(result)
            return result
    '''))
    definition = app / "workflows" / "probe.yaml"
    definition.write_text(textwrap.dedent('''\
        name: installation_probe
        agent_runtime: smolagents
        description: Exercise installed Application execution.
        model_type: probe
        max_steps: 3
        toolsets: []
        tools:
          - name: write_probe
            module: applications.nested.probe.tools
            function: write_probe
        workflow: Call write_probe with external-tool-ok, then finish.
    '''))
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["AGENTLOOM_PROJECT_ROOT"] = str(project)
    checks: list[str] = []

    def run(args, *, cwd=outside, child_env=env, input=None):
        result = subprocess.run(args, cwd=cwd, env=child_env, input=input,
                                text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"
        return result.stdout

    identity = json.loads(run([sys.executable, "-I", "-c", textwrap.dedent('''\
        import importlib.util, json, sys
        import agentloom
        from importlib.resources import files
        from agentloom.configuration import C
        from agentloom.tools.loader import resolve_tool_function
        from agentloom.utils.dynamic_import import load_function
        assert importlib.util.find_spec('src') is None
        assert importlib.util.find_spec('agentloom._compat') is None
        assert not any(type(f).__name__ == '_LegacyFinder' for f in sys.meta_path)
        assert load_function('agentloom.tools.file_ops.read_file.read_file', 'read_file') is resolve_tool_function('read_file')
        root = files('agentloom')
        assert root.joinpath('runtime/prompts/toolcalling_agent.example.yaml').read_text()
        queries = list(root.joinpath('tools/queries').rglob('*.scm'))
        assert len(queries) == 112, len(queries)
        print(json.dumps({'package_origin': agentloom.__file__, 'project_root': str(C.agent_root), 'queries': len(queries)}))
    ''')]))
    assert identity["project_root"] == str(project)
    checks += ["canonical-only imports", "explicit project context", "dynamic builtin identity", "bundled resources"]
    run([sys.executable, "-I", "-m", "agentloom", "--help"])
    console = Path(sys.executable).parent / "loom"
    run([str(console), "--help"])
    checks += ["module CLI", "console CLI"]
    script = outside / "deep" / "output" / "entry 'quoted'.py"
    run([str(console), "create", str(definition), "-o", str(script)])
    assert "sys.path" not in script.read_text()
    helper = outside / "run-model-probe.py"
    helper.write_text(MODEL_RUNNER)
    script_env = env.copy()
    script_env.pop("AGENTLOOM_PROJECT_ROOT")
    execution = run([sys.executable, "-I", str(helper), str(script), str(project / "probe-artifact.txt"), str(project)], child_env=script_env)
    assert "GENERATED_APPLICATION_PASS" in execution
    manifests = list(runtime.rglob("manifest.json"))
    assert manifests, "real runner did not allocate Run evidence"
    checks += ["generated scaffold actual runner", "external Application tool and relative import", "Run manifest"]
    # Inspected projects cannot replace the installed Studio/runtime package.
    shadow = project / "agentloom"
    shadow.mkdir()
    shadow_marker = project / "shadow-executed"
    (shadow / "__init__.py").write_text(f"from pathlib import Path\nPath({str(shadow_marker)!r}).touch()\nraise RuntimeError('shadow')\n")
    shadow_env = env.copy()
    shadow_env["PYTHONPATH"] = str(project)
    response = run([sys.executable, "-I", "-u", "-m", "agentloom.tui_bridge"], cwd=project, child_env=shadow_env,
                   input=json.dumps({"id": "probe", "method": "bootstrap", "params": {}}) + "\n")
    rows = [json.loads(line) for line in response.splitlines()]
    assert rows and rows[0]["ok"] is True, rows
    assert not shadow_marker.exists()
    checks += ["isolated Studio RPC", "project/PYTHONPATH shadow rejection"]
    return {"checks": checks, **identity, "run_manifests": [str(p) for p in manifests]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.workspace.resolve()), indent=2))
