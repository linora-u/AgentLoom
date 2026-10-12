"""Application resource paths relative to the declaring configuration root."""

from pathlib import Path

from agentloom.config.paths import configuration_root, resolve_config_path


def worker_reference_candidate(
    path_value: str,
    worker_folder: Path,
    *,
    project_root: Path | str,
) -> Path:
    """Return the absolute lexical path selected by one Worker reference."""

    value = path_value.strip()
    configured = Path(value).expanduser()
    if not configured.suffix:
        raise ValueError(
            f"worker_agents path '{value}' is missing a file extension; "
            f"must end with .yaml or .yml (e.g. '{value}.yaml')"
        )
    root = configuration_root(worker_folder.parent / "agent.yaml", project_root)
    return resolve_config_path(value, root)


def resolve_worker_reference(path_value: str, worker_folder: Path, *, project_root: Path | str) -> Path:
    candidate = worker_reference_candidate(
        path_value,
        worker_folder,
        project_root=project_root,
    )
    root = Path(project_root).expanduser().resolve()
    # Inspect the lexical path before resolve() erases symlink components.
    # Explicit absolute references are supported, but a symlink must not turn
    # a project-contained declaration into an unexpected external definition.
    try:
        parts = candidate.relative_to(root).parts
        current = root
    except ValueError:
        parts = candidate.parts[1:]
        current = Path(candidate.anchor)
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Agent path contains a symlink: {current}")
    return candidate.resolve()
