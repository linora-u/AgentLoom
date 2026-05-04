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
            
        root_dir = self.workflows_dir.parents[1]
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
    parts = yaml_path.parts
    
    # Find the index of 'applications' in the path.
    try:
        app_index = parts.index('applications')
        # category should appear right after 'applications'.
        if app_index + 1 < len(parts):
            category = parts[app_index + 1]
            return category
        else:
            raise ValueError(f"Cannot infer category from path: {yaml_path}")
    except ValueError:
        raise ValueError(f"'applications' directory was not found in path: {yaml_path}")
