"""Improvement sessions are ordinary configured Agents, including the built-ins."""
from __future__ import annotations

from pathlib import Path

from .models import TrialContext, bind_trial_context, json_value

ROLE_NAMES = ("general", "domain", "compose", "select", "reflect")
_RESOURCE_ROOT = Path(__file__).parent / "resources" / "roles"


def validate_role_output(role: str, value):
    import yaml
    from agentloom.execution.agent_runtime import OutputContract

    schema = yaml.safe_load((_RESOURCE_ROOT / f"{role}.yaml").read_text())["output_schema"]
    output = OutputContract(name=f"harness_{role}", schema=schema).validate(json_value(value))
    if role in {"general", "domain", "compose"}:
        # The model wire contract has closed objects for strict structured
        # output. Keep the existing snapshot edit mapping inside the controller.
        changes = {}
        for edit in output["edits"]:
            if edit["path"] in changes:
                raise ValueError("Role edits must contain each path only once")
            changes[edit["path"]] = edit["content"]
        output["edits"] = changes
    return output


def execute_role(payload: dict, bundle: dict, context: dict) -> dict:
    """Trusted child-process factory; normal YAML, Runtime and cleanup paths apply."""
    import yaml
    from agentloom.app import Application
    from agentloom.app.factory import YamlAgentFactory
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.trace.task_context import bind_root_run, require_root_run_state

    from .snapshot import load_bundle
    from .usage import read_run_usage

    archived = load_bundle(Path(bundle["root"]))
    trial = TrialContext.from_dict(context)
    role = payload["role"]
    if role not in ROLE_NAMES:
        raise ValueError("Unknown improvement role")
    with bind_trial_context(trial), bind_config(load_project_config(archived.project_root)), Application.open(
        archived.application_root, project_root=archived.project_root, file_logging=False
    ) as app:
        with bind_root_run(trial.trial_id):
            require_root_run_state().get_or_create_memory_snapshot(lambda: trial.memory_snapshot)
            override = payload.get("definition", "")
            item = WorkItem(payload["stage"], {"evidence": payload["evidence"]})
            if override:
                outcome = app.worker(override).invoke(item)
            else:
                # The packaged fallback is loaded through the ordinary YAML
                # normalizer and invocation path, without a direct model API.
                definition = yaml.safe_load((_RESOURCE_ROOT / f"{role}.yaml").read_text())
                definition["system_prompt"] = (_RESOURCE_ROOT / f"{role}.md").read_text()
                definition = YamlAgentFactory._prepare_agent_config(definition)
                outcome = app._invoke_definition(definition, item, 0)
            receipt = outcome.to_dict()
            role_run_dir = app.run.run_dir
            receipt["run"] = {
                "application_id": app.run.application_id,
                "task_id": app.run.task_id,
                "run_id": app.run.run_id,
                "manifest_path": str(app.run.manifest_path),
            }
    # Application teardown flushes the persisted runtime event sink. Read only
    # after that boundary, so this includes the terminal usage observation.
    receipt["usage_observations"] = [item.to_dict() for item in read_run_usage(role_run_dir)]
    if outcome.status != "success" or outcome.cleanup_errors:
        return {"status": "failed", "receipt": receipt, "error": str(receipt.get("error") or "role cleanup failed")}
    try:
        output = validate_role_output(role, outcome.output)
    except (TypeError, ValueError) as error:
        return {"status": "failed", "receipt": receipt, "error": str(error)}
    return {"status": "completed", "output": output, "receipt": receipt}


def candidate_files(bundle) -> dict[str, str]:
    """Give built-in editors actual editable text, not inaccessible host paths."""
    result = {}
    for path in sorted(bundle.project_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in {".yaml", ".yml", ".md", ".txt", ".json", ".j2"}:
            continue
        try:
            result[path.relative_to(bundle.project_root).as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return result


def preflight_roles(bundle, policy):
    """Check every frozen ordinary role and its Worker tree before paid work."""
    import yaml
    from agentloom.app.composition import build_builtin_runtime_registry
    from agentloom.app.definition import inspect_application_definition, load_prepared_agent_definition
    from agentloom.config.config import bind_config, load_project_config

    config = load_project_config(bundle.project_root)
    registry = build_builtin_runtime_registry()
    checked = set()
    with bind_config(config):
        for role in ROLE_NAMES:
            override = getattr(policy.roles, role)
            if override:
                source = bundle.application_root / override
                definition = load_prepared_agent_definition(source, project_root=bundle.project_root)
            else:
                source = bundle.application_root / "workflows" / f"harness_{role}.yaml"
                definition = yaml.safe_load((_RESOURCE_ROOT / f"{role}.yaml").read_text())
                definition["system_prompt"] = (_RESOURCE_ROOT / f"{role}.md").read_text()
            inspection = inspect_application_definition(
                bundle.project_root, str(source), definition, root_is_worker=True, base_config=config
            )
            if inspection.errors:
                raise ValueError(f"Invalid frozen improvement role {role}: " + "\n".join(inspection.errors))
            for node in inspection.definitions.values():
                runtime = node["agent_runtime"]
                if runtime not in checked:
                    registry.preflight(runtime)
                    checked.add(runtime)
