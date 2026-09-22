"""Definition-source and project-relative Application resource paths."""

from pathlib import Path


def worker_reference_candidate(
    path_value: str,
    worker_folder: Path,
    *,
    project_root: Path | str,
) -> Path:
    """Return the absolute lexical path selected by one Worker reference."""

    value = path_value.strip()
    configured = Path(value).expanduser()
    root = Path(project_root).expanduser().resolve()
    if configured.is_absolute():
        candidate = configured
    elif value.startswith(("./", "../", "worker_agents/")):
        candidate = worker_folder.parent / configured
    elif "/" in value or "\\" in value:
        candidate = root / configured
    elif configured.suffix:
        candidate = worker_folder / configured
    else:
        raise ValueError(
            f"worker_agents path '{value}' is missing a file extension; "
            f"must end with .yaml, .yml, or .md (e.g. '{value}.yaml')"
        )
    return candidate.absolute()


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
