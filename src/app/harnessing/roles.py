"""Pi transport for upstream file-edit sessions, through ordinary Worker execution."""
from __future__ import annotations

from pathlib import Path

from .models import TrialContext, bind_trial_context

ROLE_NAMES = ("general", "domain", "compose", "select", "reflect")
_RESOURCE_ROOT = Path(__file__).parent / "resources" / "roles"


def role_definition(bundle, role, reference=""):
    import yaml
    from agentloom.app.definition import load_prepared_agent_definition
    if role not in ROLE_NAMES:
        raise ValueError("Unknown improvement role")
    if reference:
        definition = load_prepared_agent_definition(bundle.application_root / reference,
                                                     project_root=bundle.project_root)
    else:
        definition = yaml.safe_load((_RESOURCE_ROOT / f"{role}.yaml").read_text())
    if definition.get("agent_runtime") != "pi":
        raise ValueError("Every improvement role must use the Pi runtime")
    # Method instructions/output grammar belong to upstream. YAML retains the
    # model, parameters, and declared tool/execution configuration.
    definition.pop("output_schema", None)
    definition["input_schema"] = {"type": "object", "properties": {"mission": {"type": "string"}},
                                  "required": ["mission"], "additionalProperties": False}
    definition["task"] = "{mission}"
    definition["self_learning"] = {"enabled": False}
    return definition


def execute_role(payload: dict, bundle: dict, context: dict) -> dict:
    """Trusted child factory. Pi's actual cwd is the isolated session workspace."""
    from agentloom.app import Application
    from agentloom.app.factory import YamlAgentFactory
    from agentloom.config.config import UnifiedConfig, bind_config, load_project_config
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.trace.task_context import bind_root_run, require_root_run_state
    from .snapshot import load_bundle
    from .usage import read_run_usage

    archived = load_bundle(Path(bundle["root"]))
    trial = TrialContext.from_dict(context)
    config = load_project_config(archived.project_root)
    with bind_trial_context(trial), bind_config(config), Application.open(
        archived.application_root, project_root=archived.project_root, file_logging=False
    ) as app:
        with bind_root_run(trial.trial_id):
            require_root_run_state().get_or_create_memory_snapshot(lambda: "")
            definition = role_definition(archived, payload["role"], payload.get("definition", ""))
            definition["system_prompt"] = payload["system"]
            definition = YamlAgentFactory._prepare_agent_config(definition)
            # RuntimeDefinition.project_root supplies the native tool cwd. The
            # frozen role/model definition has already been loaded above.
            app.project_config = UnifiedConfig(config.raw, trial.workspace_root, config.llm)
            outcome = app._invoke_definition(definition, WorkItem(payload["stage"], {"mission": payload["mission"]}), 0)
            receipt = outcome.to_dict()
            run_dir = app.run.run_dir
            receipt["run"] = {"application_id": app.run.application_id, "task_id": app.run.task_id,
                              "run_id": app.run.run_id, "manifest_path": str(app.run.manifest_path)}
    receipt["usage_observations"] = [item.to_dict() for item in read_run_usage(run_dir)]
    if outcome.status != "success" or outcome.cleanup_errors:
        return {"status": "failed", "receipt": receipt, "error": "Pi role execution or cleanup failed"}
    if not isinstance(outcome.output, str) or not outcome.output.strip():
        return {"status": "failed", "receipt": receipt, "error": "Pi role returned no textual reply"}
    return {"status": "completed", "output": outcome.output, "receipt": receipt}


def preflight_roles(bundle, policy):
    from agentloom.app.composition import build_builtin_runtime_registry
    from agentloom.app.definition import inspect_application_definition
    from agentloom.config.config import bind_config, load_project_config
    config = load_project_config(bundle.project_root)
    with bind_config(config):
        registry = build_builtin_runtime_registry()
        for role in ROLE_NAMES:
            reference = getattr(policy.roles, role)
            definition = role_definition(bundle, role, reference)
            definition["system_prompt"] = "Upstream Harness² supplies the method instructions for this session."
            source = bundle.application_root / (reference or f"workflows/harness_{role}.yaml")
            inspection = inspect_application_definition(bundle.project_root, str(source), definition,
                                                       root_is_worker=True, base_config=config)
            if inspection.errors:
                raise ValueError(f"Invalid frozen improvement role {role}: " + "\n".join(inspection.errors))
            registry.preflight("pi")
