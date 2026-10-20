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
    return OutputContract(name=f"harness_{role}", schema=schema).validate(json_value(value))


def execute_role(payload: dict, bundle: dict, context: dict) -> dict:
    """Trusted child-process factory; normal YAML, Runtime and cleanup paths apply."""
    import yaml
    from agentloom.app import Application
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.app.factory import YamlAgentFactory
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
            receipt["usage_observations"] = [item.to_dict() for item in read_run_usage(app.run.run_dir)]
            receipt["run"] = {
                "application_id": app.run.application_id,
                "task_id": app.run.task_id,
                "run_id": app.run.run_id,
                "manifest_path": str(app.run.manifest_path),
            }
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
