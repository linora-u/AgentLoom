# Full Example: Creating a `code_review` Application from Scratch

> This document demonstrates a complete workflow from requirements gathering to final file generation, serving as a reference sample for Skill execution.
>
> **All paths are relative to the project root directory** (i.e., the directory containing `config/llm.yaml`).

---

## Example Scenario

User requirement: "Help me create a Code Review Application that can perform multi-dimensional code review on a specified Git repository"

---

## Phase 1: Requirements Gathering Results

| Question | User's Answer |
|----------|--------------|
| Q1 Application name | `code_review` |
| Q2 One-line description | `Perform multi-dimensional code review on a Git repository and output a structured review report` |
| Q3 Worker stages | 3 stages: ① Code scanning & structure analysis ② Risk assessment & issue tagging ③ Report summary |
| Q4 model_type available options discovery | Read `config/llm.yaml`, discovered available types: `powerful` / `fast` / `summary`, `default_model_type=powerful` |
| Q5 Supervisor model_type strategy | Use configured `default_model_type` (not explicitly specified) |
| Q6 Worker model_type strategy | Same as Supervisor (not explicitly specified) |
| Q7 tool_call_type | `code_act` |
| Q8 Custom tools | One `get_repo_context` tool to get repository overview |
| Q9 Worker tools | `read_file`, `get_file_outline`, `ripgrep_search_directory` |
| Q10 planning_interval | Worker set to `3` |
| Q11 max_steps | Default `80` |
| Q12 App-level config | Not needed |
| Q13 Custom sysprompt | Not needed |
| Q14 execution_env | `local` |
| Q15 Private skills | Not needed |

---

## Phase 2: Plan Confirmation

```
📋 Application Generation Plan

Name: code_review
Description: Perform multi-dimensional code review on a Git repository and output a structured review report
Mode: Supervisor + 3 Workers

Directory structure preview:
applications/code_review/
├── code_review_app.py
├── agent_tools/
│   └── repo_context.py
└── workflows/
    ├── code_review_agent.yaml
    └── worker_agents/
        ├── code_scan.yaml
        ├── risk_assessment.yaml
        └── report_generation.yaml

Supervisor configuration summary:
- name: code_review_agent
- model_type: Uses configured default_model_type (not explicitly specified)
- tool_call_type: code_act
- Custom tools: get_repo_context
- Worker count: 3

Worker configuration summary:
| Stage | Name                    | Responsibility              | model_type Strategy | Tools                                                   |
|-------|-------------------------|-----------------------------|---------------------|---------------------------------------------------------|
| 1     | code_scan               | Code scanning & structure analysis | Use configured default | read_file, get_file_outline, ripgrep_search_directory |
| 2     | risk_assessment         | Risk assessment & issue tagging    | Use configured default | read_file, ripgrep_search_directory             |
| 3     | report_generation       | Report summary              | Use configured default | write_markdown_file                                      |
```

User confirmation: ✅

---

## Phase 3: Generated Files

### File 1: `applications/code_review/workflows/code_review_agent.yaml`

```yaml
name: "code_review_agent"
description: |
  As the Code Review Supervisor Agent, your core responsibility is to coordinate multiple
  specialized Worker Agents to perform multi-dimensional code review on the target Git
  repository and produce a structured review report.

tool_call_type: "code_act"

workflow: |
  # Code Review Workflow

  ## Background
  You are a **senior code review architect**. The current task is to perform a systematic
  multi-dimensional review of the target code repository.

  ## Execution Flow

  ```mermaid
  flowchart TD
    A[Call get_repo_context to get repository overview] --> B[Call code_scan for code scanning]
    B --> C[Call risk_assessment for risk assessment]
    C --> D[Call report_generation for report generation]
    D --> E[Output final review report]
  ```

  ## Execution Principles
  1. **Get context first**: Must call `get_repo_context` first to understand overall repository structure
  2. **Call Workers sequentially**: Strictly execute in order step0 → step1 → step2
  3. **Pass context**: Each Worker's output serves as input for the next Worker
  4. **Summarize report**: Present step2's output as the complete review report

  ## Output Requirements
  - Output the complete Markdown report returned by report_generation
  - If a Worker fails, report the failed stage and reason

tools:
  - name: "read_file"
  - name: "browse_directory"
  - name: "get_repo_context"
    module: "applications.code_review.agent_tools.repo_context"
    function: "get_repo_context"

# Worker YAML must be in workflows/worker_agents/ directory
# Shorthand form (recommended for same-app workers):
worker_agents:
  - path: "code_scan.yaml"
  - path: "risk_assessment.yaml"
  - path: "report_generation.yaml"
# Equivalent full relative path form (for cross-app references):
#  - path: "applications/code_review/workflows/worker_agents/code_scan.yaml"
#  - path: "applications/code_review/workflows/worker_agents/risk_assessment.yaml"
#  - path: "applications/code_review/workflows/worker_agents/report_generation.yaml"

execution_env:
  type: "local"
```

---

### File 2: `applications/code_review/workflows/worker_agents/code_scan.yaml`

```yaml
name: "code_scan"
description: "Code structure scanning and analysis Worker"
tool_call_type: "code_act"
planning_interval: 3
max_steps: 40

tools:
  - name: "read_file"
  - name: "get_file_outline"
  - name: "ripgrep_search_directory"
  - name: "browse_directory"

workflow: |
  # Code Scanning & Structure Analysis

  ## Background
  You are a **senior code structure analysis engineer**, responsible for performing
  a comprehensive scan of the target code repository.

  ## Core Responsibilities
  1. **Directory structure scanning**: List all source directories and key files
  2. **Module dependency analysis**: Identify import dependencies between modules
  3. **Code scale statistics**: File count, lines of code, number of functions/classes
  4. **Architecture pattern identification**: Identify design patterns and architectural styles in use

  ## Constraints
  - ❌ Do not perform any risk assessment or quality judgment (that is risk_assessment's responsibility)
  - ❌ Do not modify any source code files
  - ✅ All analysis must be based on actual code content — no speculation

  ## Output Requirements
  - Format: Structured Markdown text
  - Must include: File tree, module dependency graph, code scale statistics table
  - Mark uncertain inferences with 【Inferred】

agent_function_schema:
  description: |
    Code structure scanning and analysis tool. Scans the target code repository's
    directory structure, module dependencies, and code scale, outputting a structured
    analysis report.
  inputs:
    query:
      description: "Scan task description, including target repository path and focus areas"
      required: true
  output:
    description: "Markdown-formatted code structure analysis report"
```

---

### File 3: `applications/code_review/workflows/worker_agents/risk_assessment.yaml`

```yaml
name: "risk_assessment"
description: "Risk assessment and issue tagging Worker"
tool_call_type: "code_act"
planning_interval: 3
max_steps: 40

tools:
  - name: "read_file"
  - name: "ripgrep_search_directory"
  - name: "read_file"

workflow: |
  # Risk Assessment & Issue Tagging

  ## Background
  You are a **senior code security and quality audit engineer**, responsible for
  conducting in-depth inspection of risk points and quality issues in the code
  based on the structure analysis results from code_scan.

  ## Core Responsibilities
  1. **Security risk scanning**: SQL injection, XSS, hardcoded credentials, insecure dependencies
  2. **Code quality issues**: Duplicate code, overly long functions, excessive complexity, missing error handling
  3. **Best practice violations**: Naming conventions, missing comments, irregular logging
  4. **Severity tagging**: Tag each issue as Critical / High / Medium / Low

  ## Constraints
  - ❌ Do not modify any source code files
  - ❌ Do not provide fix suggestions (that is report_generation's responsibility)
  - ✅ Each issue must include precise `file:line_number` location
  - ✅ Mark uncertain issues with confidence level (High/Medium/Low)

  ## Output Requirements
  - Format: Issue checklist table
  - Must include: Issue ID, severity, issue description, file:line_number, confidence level
  - Sort by severity in descending order

agent_function_schema:
  description: |
    Code risk assessment and issue tagging tool. Based on code structure analysis
    results, performs in-depth inspection of security risks and quality issues,
    outputting an issue checklist with severity tags.
  inputs:
    query:
      description: "Review task description, including step0 analysis result summary and review focus areas"
      required: true
  output:
    description: "Markdown-formatted risk assessment report (issue checklist table)"
```

---

### File 4: `applications/code_review/workflows/worker_agents/report_generation.yaml`

```yaml
name: "report_generation"
description: "Review report summary generation Worker"
tool_call_type: "code_act"
planning_interval: 3
max_steps: 40

tools:
  - name: "write_markdown_file"

workflow: |
  # Step 2: Report Summary

  ## Background
  You are a **technical writing expert**, responsible for consolidating the analysis
  results from preceding Workers into a well-structured, readable code review report.

  ## Core Responsibilities
  1. **Consolidate and integrate**: Merge outputs from code_scan (structure analysis) and risk_assessment (risk assessment)
  2. **Priority ranking**: Order issues by business impact and fix cost
  3. **Fix suggestions**: Provide specific fix suggestions for each Critical/High issue
  4. **Executive summary**: Write a management-oriented executive summary at the beginning of the report

  ## Constraints
  - ❌ Do not omit any Critical/High severity issues
  - ❌ Do not fabricate issues not mentioned in code_scan/risk_assessment
  - ✅ Fix suggestions must be specific to the code level (function names, file paths)

  ## Output Format

  ```markdown
  # Code Review Report

  ## Executive Summary
  <2-3 sentences summarizing review conclusions>

  ## 1. Code Structure Overview
  <From step0's structure analysis>

  ## 2. Issue Checklist
  ### 2.1 Critical
  ### 2.2 High
  ### 2.3 Medium
  ### 2.4 Low

  ## 3. Fix Suggestions
  <Specific fix plans for Critical/High issues>

  ## 4. Summary & Next Steps
  ```

agent_function_schema:
  description: |
    Review report summary generation tool. Consolidates code structure analysis
    and risk assessment results into a complete Markdown-formatted code review report.
  inputs:
    query:
      description: "Report generation task, including complete outputs from step0 and step1"
      required: true
  output:
    description: "Complete Markdown-formatted code review report"
```

---

### File 5: `applications/code_review/agent_tools/repo_context.py`

```python
"""
Repository context generation tool.

Dynamically loaded tools are plain Python functions — no decorators are needed.
The framework dynamically imports them via the module + function configuration in YAML.
Tool descriptions are automatically extracted from the function's docstring.
"""

import os
import subprocess


def get_repo_context(repo_path: str) -> str:
    """
    Get an overview of the target Git repository's context information.

    Scans the repository directory structure, Git info, README content, etc.,
    to provide foundational context for subsequent code review.

    Args:
        repo_path: Path to the target repository (relative or absolute)

    Returns:
        Markdown-formatted repository overview text, including directory structure,
        Git branch info, README summary, etc.
    """
    abs_path = os.path.abspath(repo_path)
    if not os.path.isdir(abs_path):
        return f"Error: Path {abs_path} does not exist or is not a directory"

    sections = []
    sections.append(f"# Repository Overview: {os.path.basename(abs_path)}\n")

    # Directory structure (max 3 levels)
    try:
        result = subprocess.run(
            ["find", abs_path, "-maxdepth", "3", "-type", "f", "-name", "*.py"],
            capture_output=True, text=True, timeout=10
        )
        py_files = result.stdout.strip().split("\n")
        sections.append(f"## Python File Count: {len(py_files)}\n")
    except Exception as e:
        sections.append(f"## File Scan Failed: {e}\n")

    # Git info
    try:
        branch = subprocess.run(
            ["git", "-C", abs_path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5
        )
        sections.append(f"## Current Branch: {branch.stdout.strip()}\n")
    except Exception:
        sections.append("## Git Info: Unavailable\n")

    # README
    readme_path = os.path.join(abs_path, "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read(2000)  # Only read first 2000 characters
        sections.append(f"## README Summary\n{content}\n")

    return "\n".join(sections)
```

---

### File 6: `applications/code_review/code_review_app.py`

```python
#!/usr/bin/env python3
"""
code_review – Application entry script.

How to run:
    PROJECT_ROOT=/path/to/project
    cd "$PROJECT_ROOT"
    .venv/bin/python applications/code_review/code_review_app.py

Or using runner:
    PROJECT_ROOT=/path/to/project
    cd "$PROJECT_ROOT"
    .venv/bin/python src/runner.py applications/code_review/workflows/code_review_agent.yaml

Or using AgentLoom CLI (if installed):
    cd "$PROJECT_ROOT"
    loom run applications/code_review/workflows/code_review_agent.yaml
"""

import os
import sys

# Ensure the project root directory is in sys.path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.runner import run_app


def main():
    run_app("applications/code_review/workflows/code_review_agent.yaml")


if __name__ == "__main__":
    main()
```

---

## Phase 4: Running Guide

```bash
# Method 1: Using runner (recommended)
cd <project_root>
<python> src/runner.py applications/code_review/workflows/code_review_agent.yaml

# Method 2: Using the entry script
cd <project_root>
<python> applications/code_review/code_review_app.py
```

---

## Single Agent Mode Example: `simple_scanner`

If you only need a single Agent working independently without Supervisor/Worker orchestration:

### Directory Structure

```
applications/simple_scanner/
├── simple_scanner_app.py
└── workflows/
    └── simple_scanner_agent.yaml    # Standalone Agent, no Supervisor orchestration
```

### `simple_scanner_agent.yaml`

```yaml
name: "simple_scanner"
description: "A simple code scanning Agent that runs independently without Supervisor orchestration"
model_type: "fast"
tool_call_type: "code_act"
max_steps: 30

tools:
  - name: "read_file"
  - name: "browse_directory"
  - name: "list_files_glob"
  - name: "ripgrep_search_directory"

workflow: |
  # Simple Code Scan

  ## Background
  You are a **code scanning assistant**, responsible for quickly scanning code files
  in a specified directory.

  ## Tasks
  1. Browse the target directory structure
  2. Count the number of files and lines of code for each language
  3. Output a concise scan report

  ## Output Requirements
  - Markdown format
  - Include file statistics table and directory structure tree

# Single Agent mode: No worker_agents or agent_function_schema needed, run directly with runner
```

### How to Run

```bash
cd <project_root>
<python> src/runner.py applications/simple_scanner/workflows/simple_scanner_agent.yaml
```
