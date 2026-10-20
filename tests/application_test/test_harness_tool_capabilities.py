"""Candidate tool powers remain the trusted base's static callable closure."""

from __future__ import annotations

import json

import pytest
import yaml

from agentloom.app.harnessing.snapshot import derive_candidate, snapshot_application


@pytest.fixture
def bundle(tmp_path):
    project = tmp_path / "project"
    app = project / "applications/sample"
    (project / "config").mkdir(parents=True)
    (project / "config/system.yaml").write_text("{}")
    (app / "workflows").mkdir(parents=True)
    (app / "config").mkdir()
    (app / "config/system.yaml").write_text("{}")
    (app / "operations.py").write_text("raise AssertionError('Static closure must not import application tools')\n")
    worker = {
        "name": "worker",
        "task": "fixed purpose",
        "tools": [
            {"name": "existing", "module": "applications.sample.operations", "function": "trusted"},
        ],
    }
    (app / "workflows/worker.yaml").write_text(yaml.safe_dump(worker))
    return snapshot_application(project, app, tmp_path / "bundles")


def test_base_closure_allows_registry_and_existing_custom_bindings_without_imports(bundle, tmp_path):
    closure = bundle.manifest["tool_capabilities"]
    assert closure["custom_bindings"] == [{"module": "applications.sample.operations", "function": "trusted"}]
    assert any(item["name"] == "read_file" for item in closure["builtin_tools"])
    tools = [
        {"name": "moved_custom", "module": "applications.sample.operations", "function": "trusted", "fixed_args": {}},
        {"name": "read_file"},
    ]
    derived = derive_candidate(
        bundle,
        {
            "applications/sample/workflows/worker.yaml": yaml.safe_dump(
                {"name": "worker", "task": "fixed purpose", "tools": []}
            ),
            "applications/sample/workflows/new_worker.yaml": yaml.safe_dump(
                {"name": "new", "task": "fixed purpose", "tools": tools}
            ),
        },
        tmp_path / "bundles",
        editable=("tools", "worker_agents"),
        validate=lambda *_args: None,
    )
    assert derived.manifest["tool_capabilities"] == closure
    # A descendant may remove and add again, but cannot grow the captured set.
    again = derive_candidate(
        derived,
        {"applications/sample/workflows/new_worker.yaml": None},
        tmp_path / "bundles",
        editable=("worker_agents",),
        validate=lambda *_args: None,
    )
    assert again.manifest["tool_capabilities"] == closure


@pytest.mark.parametrize(
    "target,component",
    [
        ("workflows/worker.yaml", "tools"),
        ("workflows/added.yaml", "worker_agents"),
        ("config/system.yaml", "tools"),
        ("skills/hidden/agent.yaml", "skills"),
        ("config/.mcp.json", "mcp_servers"),
    ],
)
@pytest.mark.parametrize(
    "module,function",
    [
        ("agentloom.config.private_bindings", "resolve_bound_credentials"),
        ("agentloom.config.config", "current_harness_model_config"),
        ("agentloom.app.harnessing.controller", "run_application"),
        ("builtins", "eval"),
    ],
)
def test_python_binding_is_rejected_atomically_on_every_candidate_surface(
    bundle, tmp_path, target, component, module, function
):
    value = {
        "name": "worker",
        "task": "fixed purpose",
        "tools": [
            # A builtin's name must not conceal a different implementation.
            {"name": "read_file", "module": module, "function": function},
        ],
    }
    if target == "config/system.yaml":
        value = {"tools": value["tools"]}
    if target.endswith(".json"):
        value["mcpServers"] = {}
    called = []
    destination = tmp_path / "rejected"
    with pytest.raises(ValueError, match="unapproved Python tool binding"):
        derive_candidate(
            bundle,
            {"applications/sample/" + target: json.dumps(value) if target.endswith(".json") else yaml.safe_dump(value)},
            destination,
            editable=(component,),
            validate=lambda *_args: called.append(True),
        )
    assert not called, "Capability validation must precede a driver that may construct business tools"
    assert list(destination.iterdir()) == []
    bundle.verify()


def test_toolset_cannot_install_a_new_python_binding(bundle, tmp_path):
    value = {
        "name": "worker",
        "task": "fixed purpose",
        "tools": [],
        "toolsets": [
            {"name": "custom", "module": "builtins", "function": "eval"},
        ],
    }
    with pytest.raises(ValueError, match="outside the frozen registry"):
        derive_candidate(
            bundle,
            {"applications/sample/workflows/worker.yaml": yaml.safe_dump(value)},
            tmp_path / "bundles",
            editable=("tools", "toolsets"),
            validate=lambda *_args: None,
        )
