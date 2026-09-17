"""Small bounded tools that operate on a fresh acceptance fixture, never a checkout.

Tools contain no repair, test-generation template or oracle. All source changes,
regression assertions and reports are authored by the real model-backed Workers.
The host owns the workspace marker and append-only ledger outside the workspace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree


def _workspace(workspace: str) -> Path:
    root = Path(workspace).resolve()
    expected = os.environ.get("AGENTLOOM_ARCHITECTURE_WORKSPACE")
    if not expected or root != Path(expected).resolve():
        raise ValueError("workspace must be the host-allocated acceptance workspace")
    if not (root / ".architecture-case.json").is_file():
        raise ValueError("missing acceptance workspace marker")
    return root


def _path(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    if root not in path.parents or Path(relative_path).is_absolute():
        raise ValueError("path must remain inside the acceptance workspace")
    if path.name.startswith(".") or "__pycache__" in path.parts:
        raise ValueError("hidden files and bytecode are not task artifacts")
    return path


def _ledger(root: Path, operation: str, **values: object) -> None:
    from agentloom.runtime.trace.task_context import capture_explicit_execution_context

    context = capture_explicit_execution_context()
    marker = json.loads((root / ".architecture-case.json").read_text())
    event = {
        "at": datetime.now(UTC).isoformat(), "operation": operation,
        "case_nonce": marker["case_nonce"], "workspace": str(root),
        **{name: getattr(context, name) for name in (
            "task_id", "agent_name", "root_run_id", "local_run_id", "runtime_agent_path"
        )},
        **values,
    }
    with (root.parent / "tool-ledger.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def inspect_workspace(workspace: str) -> str:
    """List the controlled repository's source, configuration, tests and contract.

    Args:
        workspace: Absolute workspace path from the supervisor task.
    """
    root = _workspace(workspace)
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if p.is_file() and not any(part.startswith(".") or part == "__pycache__"
                                              for part in p.relative_to(root).parts))
    _ledger(root, "inspect", files=files)
    return json.dumps({"workspace": str(root), "files": files})


def read_workspace_file(workspace: str, relative_path: str) -> str:
    """Read one actual source, configuration, test, contract or report file.

    Args:
        workspace: Absolute workspace path from the supervisor task.
        relative_path: File path relative to workspace, such as CONTRACT.md.
    """
    root = _workspace(workspace)
    path = _path(root, relative_path)
    if path.stat().st_size > 100_000:
        raise ValueError("file exceeds acceptance read limit")
    content = path.read_text(encoding="utf-8")
    _ledger(root, "read", path=relative_path, sha256=hashlib.sha256(content.encode()).hexdigest())
    return content


def write_workspace_file(workspace: str, relative_path: str, content: str) -> str:
    """Write model-authored source, generated regression tests or a structured report.

    Existing baseline tests, configuration materials and contract are immutable.

    Args:
        workspace: Absolute workspace path from the supervisor task.
        relative_path: orderdesk/*.py, tests/generated/test_*.py or reports/*.json path.
        content: Complete UTF-8 file content authored by the Worker or Supervisor.
    """
    root = _workspace(workspace)
    path = _path(root, relative_path)
    rel = path.relative_to(root)
    allowed = (
        (rel.parts[0] == "orderdesk" and path.suffix == ".py")
        or (rel.parts[:2] == ("tests", "generated") and path.name.startswith("test_") and path.suffix == ".py")
        or (rel.parts[0] == "reports" and path.suffix in {".json", ".md"})
    )
    if not allowed or len(content.encode()) > 100_000:
        raise ValueError("write must target bounded source, generated test, or report content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode()).hexdigest()
    _ledger(root, "write", path=rel.as_posix(), sha256=digest)
    return json.dumps({"path": rel.as_posix(), "sha256": digest, "bytes": len(content.encode())})


def junit_summary(path: Path) -> dict[str, object]:
    """Read actual JUnit counts and case names, including collection errors."""
    tree = ElementTree.parse(path)
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else list(tree.getroot().iter("testsuite"))
    return {
        **{key: sum(int(suite.get(key, "0")) for suite in suites)
           for key in ("tests", "failures", "errors", "skipped")},
        "cases": [case.get("name", "") for case in tree.getroot().iter("testcase")],
    }


def run_workspace_tests(workspace: str, label: str) -> str:
    """Actually execute every existing and generated pytest test, retaining evidence.

    Args:
        workspace: Absolute workspace path from the supervisor task.
        label: Short label for this attempt, e.g. implementer or verifier.
    """
    root = _workspace(workspace)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", label):
        raise ValueError("label must contain 1..32 letters, digits, underscores or hyphens")
    report_dir = root / "reports"
    report_dir.mkdir(exist_ok=True)
    stem = f"pytest-{label}-{uuid.uuid4().hex[:8]}"
    junit_path = report_dir / f"{stem}.xml"
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    command = [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
               "tests", f"--junitxml={junit_path}"]
    completed = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=60)
    (report_dir / f"{stem}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    summary = junit_summary(junit_path) if junit_path.exists() else {"tests": 0, "errors": 1}
    result = {"exit_code": completed.returncode, "command": command, **summary,
              "junit": f"reports/{stem}.xml", "log": f"reports/{stem}.log",
              "report": f"reports/{stem}.json"}
    (root / result["report"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    _ledger(root, "pytest", **result)
    return json.dumps(result)
