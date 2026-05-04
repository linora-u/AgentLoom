#!/usr/bin/env python3
"""
AI Quality Analysis agent usage example.

Demonstrates how to use the AI Quality Analysis Supervisor Agent to run
code-quality inspection tasks. The agent coordinates multiple Micro Agents
to perform static analysis and code review.
"""

import os
import sys
# Add project root to sys.path so `src` can be imported.
# Get parent-of-parent directory of current script (project root).
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import fire
import shutil

from pathlib import Path
from src.workflows.workflow_manager import get_supervisor_agent_yaml_path

def run_ai_quality_analysis(
    project_path: str = ".",
    log_to_file: bool = False,
    resume: str | None = None,
):
    """
    Run AI Quality Analysis code-quality inspection.

    Args:
        project_path: Path to the project directory to analyze.
        log_to_file:  Whether to write logs to a file.
        resume:       Resume from a checkpoint task ID.
    """
    from src.runner import run_app

    # Resolve and validate project path.
    resolved_project = Path(project_path).resolve()
    if not resolved_project.is_dir():
        print(f"Error: Project path is not a directory: {resolved_project}")
        return False

    # Pass project path to agent tools via environment variable.
    os.environ["AI_QA_PROJECT_PATH"] = str(resolved_project)

    yaml_path = get_supervisor_agent_yaml_path("ai_quality_analysis") / "code_review_agent.yaml"
    if not yaml_path.exists():
        print(f"Error: Config file not found: {yaml_path}")
        return False

    # Clean up temp directory only for fresh runs (not resume).
    if not resume:
        temp_dir = Path(project_root) / "temp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    print(f"[ai_quality_analysis] Analyzing project: {resolved_project}")

    try:
        run_app(
            str(yaml_path),
            log_to_file=log_to_file,
            resume_task_id=resume,
        )
        return True

    except KeyboardInterrupt:
        print("\nInterrupted. Use --resume to continue.")
        return False

    except Exception as e:
        print(f"\nExecution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def cli_run_ai_quality_analysis(
    project_path: str = ".",
    log_to_file: bool = False,
    resume: str | None = None,
):
    """
    CLI entry function to run AI Quality Analysis.

    Args:
        project_path: Path to the project directory to analyze.
        log_to_file:  Whether to log to file (e.g., --log-to-file).
        resume:       Resume from a checkpoint task ID (e.g., --resume task_xxx).

    Examples:
        # Analyze a specific project
        python ai_quality_analysis_demo.py /path/to/project

        # With logging
        python ai_quality_analysis_demo.py /path/to/project --log-to-file

        # Resume after interruption
        python ai_quality_analysis_demo.py /path/to/project --resume task_xxx
    """
    success = run_ai_quality_analysis(project_path, log_to_file=log_to_file, resume=resume)
    if not success:
        print("\n❌ Code review execution failed")
        sys.exit(1)


if __name__ == "__main__":
    fire.Fire(cli_run_ai_quality_analysis)
