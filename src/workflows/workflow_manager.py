"""
Workflow manager.

Provides functionality to retrieve workflow configuration paths.
"""

from pathlib import Path

class WorkflowManager:
    """Workflow manager."""

    def __init__(self):
        """Initialize the workflow manager."""
        self.workflows_dir = Path(__file__).parent

    def get_worker_agent_yaml_path(self, category: str) -> Path:
        """Get the worker-agent configuration directory path."""
        return self.get_supervisor_agent_yaml_path(category) / 'worker_agents'

    def get_supervisor_agent_yaml_path(self, category: str) -> Path:
        """Get the supervisor-agent configuration directory path."""
            
        from src.lib.config import C
        root_dir = Path(C.agent_root)
        app_path = root_dir / 'applications' / category / 'workflows'
        if app_path.exists():
            return app_path
        else:
            raise ValueError(f"[Workflows] Workflow path for '{category}' does not exist and must be created.")


# Create a global instance.
workflow_manager = WorkflowManager()


def get_worker_agent_yaml_path(category: str) -> Path:
    """Get the worker-agent configuration directory path."""
    return workflow_manager.get_worker_agent_yaml_path(category)

def get_supervisor_agent_yaml_path(category: str) -> Path:
    """Get the supervisor-agent configuration directory path."""
    return workflow_manager.get_supervisor_agent_yaml_path(category)

def infer_category_from_yaml_path(yaml_path: Path) -> str:
    """
    Infer the category from a YAML file path.
    
    The path format should be: .../applications/{category}/workflows/xxx.yaml
    
    Args:
        yaml_path: Path to the YAML file.
        
    Returns:
        str: The inferred category.
        
    Raises:
        ValueError: Raised when the path format is not as expected.
    """
    yaml_path = Path(yaml_path).resolve()
    for ancestor in yaml_path.parents:
        if ancestor.name == 'workflows':
            app_root = ancestor.parent
            for parent in app_root.parents:
                if parent.name == 'applications':
                    return app_root.relative_to(parent).as_posix()
    raise ValueError(f"'applications' directory was not found in path: {yaml_path}")
