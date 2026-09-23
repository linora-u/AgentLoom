"""Real-provider ticket 08 Applications, with isolated configuration and evidence.

Each invocation creates a new private workspace; existing evidence is never
overwritten. Assertions inspect actual tool records and effects, not just the
model's final answer. The private llm.yaml is copied but never included in a
report. Run this script with the worktree's own interpreter.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
import yaml

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    "mcp", "mcp_nested", "mcp_error", "goal", "skill", "skill_proposal", "memory", "context",
    "outline_python", "outline_json", "ast", "lsp_symbols", "lsp_definition", "lsp_references",
    "lsp_workspace", "lsp_hover", "markdown_structured", "markdown_raw", "markdown_append",
    "worker", "parallel_workers", "goal_worker",
)


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def tool_records(runtime: Path) -> list[dict]:
    records = {}
    for path in runtime.rglob("*.json"):
        for item in walk(json.loads(path.read_text())):
            if {"call_id", "tool_name", "status", "input"} <= item.keys():
                records[(item["call_id"], item["tool_name"])] = item
    return list(records.values())


def configure_case(case: str, workspace: Path, workflow: Path, system: dict, definition: dict) -> set[str]:
    source = workspace / "sample.py"
    source.write_text("class Invoice:\n    def total(self, values):\n        return sum(values)\n\ndef compute_total(values):\n    return Invoice().total(values)\n\nRESULT = compute_total([19, 23])\n")
    report = workspace / "report.md"
    uses_mcp = {"mcp", "mcp_nested", "mcp_error", "goal", "context", "worker", "parallel_workers", "goal_worker"}
    if case not in uses_mcp:
        definition.pop("mcp_servers", None)
    expected = set()
    task = ""
    if case == "mcp":
        task = "Call mcp__facts__lookup exactly once with query='ticket08-live-mcp'. Report its returned answer and query."
        expected = {"mcp__facts__lookup"}
    elif case == "mcp_nested":
        task = "Call mcp__facts__nested_lookup(request={'query':'ticket08-nested-query','limit':1}) exactly once. Report its actual answer and query."
        expected = {"mcp__facts__nested_lookup"}
    elif case == "mcp_error":
        task = "Call mcp__facts__fail_lookup with query='intentional failure'. After observing the actual error, recover by calling mcp__facts__lookup with query='recovered'. Report both results accurately."
        expected = {"mcp__facts__lookup"}
    elif case == "goal":
        definition["goal"] = {"enabled": True}
        task = "Call get_goal. Call mcp__facts__lookup with query='goal evidence'. Once the service result is verified, call update_goal(status='complete', evidence=<actual verified answer and query>)."
        expected = {"get_goal", "update_goal", "mcp__facts__lookup"}
    elif case == "skill":
        skill = workflow.parents[1] / "skills" / "ticket-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: ticket-review\ndescription: Retrieve the verification token for ticket reviews.\n---\nThe verification token is SKILL-ACTIVE-5832. Include it in your answer.\n")
        task = "Activate the ticket-review Skill using the skill tool. Return the verification token from its actual contents."
        expected = {"skill"}
    elif case == "skill_proposal":
        content = "---\nname: future-review\ndescription: A candidate review procedure.\n---\n# Future review\nVerify a report against its source evidence.\n"
        task = f"Use skill_manage(action='create', name='future-review', content={content!r}) to submit a proposal. Report its returned path; do not promote or activate it."
        expected = {"skill_manage"}
    elif case == "memory":
        system["self_learning"] = {"enabled": True}
        task = "Use memory(action='list', scope='app') and memory(action='list', scope='project'). Then use session_search(query='prior ticket review', scope='current_app'). Report the actual results, including empty results."
        expected = {"memory", "session_search"}
    elif case == "context":
        system["context_engine"] = {"min_chars": 1000, "preview_max_chars": 300}
        task = "Call mcp__facts__context_payload(query='full artifact'). The response is a ContextRef preview. Call loom_retrieve_context with that exact ref, query='TARGET_RECORD', limit=5. Report the verification_value from the original content."
        expected = {"mcp__facts__context_payload", "loom_retrieve_context"}
    elif case.startswith("outline_"):
        path = source
        if case == "outline_json":
            path = workspace / "sample.json"
            dump(path, {"fixture_name": "ticket08", "items": [{"id": 1, "value": 42}]})
        task = f"Call get_file_outline(file_path={str(path)!r}, detail_level='full'). Report the actual structure."
        expected = {"get_file_outline"}
    elif case == "ast":
        task = f"Call ast_grep_search_file(file_path={str(source)!r}, keyword='compute_total', language='python'). Report the actual matching function."
        expected = {"ast_grep_search_file"}
    elif case.startswith("lsp_"):
        operation, arguments = {
            "lsp_symbols": ("lsp_get_document_symbols", {"file_path": str(source), "language": "python"}),
            "lsp_definition": ("lsp_find_definition", {"file_path": str(source), "line": 8, "character": 12, "language": "python"}),
            "lsp_references": ("lsp_find_references", {"file_path": str(source), "line": 5, "character": 8, "language": "python"}),
            "lsp_workspace": ("lsp_get_workspace_symbols", {"directory": str(workspace), "query": "compute_total", "language": "python"}),
            "lsp_hover": ("lsp_hover", {"file_path": str(source), "line": 5, "character": 8}),
        }[case]
        if case == "lsp_hover":
            system["lsp_servers"] = {"enabled": True, "servers": ["python"]}
        task = f"Call {operation} with these exact arguments: {json.dumps(arguments)}. Report the actual code intelligence result."
        expected = {operation}
    elif case.startswith("markdown_"):
        if case == "markdown_structured":
            task = f"Call write_markdown_file(file_path={str(report)!r}, title='Ticket 08', sections=[{{'heading':'Result','level':2,'body':'Validated structured output.'}}])."
            expected = {"write_markdown_file"}
        elif case == "markdown_raw":
            encoded = base64.b64encode(b"# Ticket 08\n\nValidated raw output.\n").decode()
            task = f"Call write_markdown_file_raw with file_path={str(report)!r} and content_b64={encoded!r}. The content_b64 argument is essential: it is the full Markdown report."
            expected = {"write_markdown_file_raw"}
        else:
            report.write_text("# Ticket 08\n\nExisting content.\n")
            task = f"Call append_markdown_sections(file_path={str(report)!r}, sections=[{{'heading':'Appendix','level':2,'body':'Validated appended output.'}}]). Preserve the existing report."
            expected = {"append_markdown_sections"}
    elif case in {"worker", "parallel_workers", "goal_worker"}:
        worker = workflow.parent / "worker_agents" / "fact_worker.yaml"
        worker.parent.mkdir()
        worker.write_text(yaml.safe_dump({
            "name": "fact_worker", "agent_runtime": "smolagents",
            "runtime_options": {"max_steps": 6, "smart_summary": False, "todo_mode": "off"},
            "description": "Look up one fact from the service.",
            "workflow": "Call mcp__facts__lookup with query equal to the supplied query input, then report its actual result using final_answer.",
            "tools": [], "toolsets": [], "mcp_servers": definition["mcp_servers"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query to send unchanged to the fact service.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }))
        definition.pop("mcp_servers")
        definition["worker_agents"] = [{"path": "fact_worker.yaml"}]
        task = "Call fact_worker(query='worker-one') and report its actual result."
        expected = {"fact_worker", "mcp__facts__lookup"}
        if case == "parallel_workers":
            task = "Call fact_worker twice, with query='worker-one' and query='worker-two'. Use parallel tool calls if available. Report both actual results."
        elif case == "goal_worker":
            definition["goal"] = {"enabled": True}
            task = "Call get_goal, then call fact_worker(query='goal-worker'). Verify its actual result, then complete the root Goal using update_goal(status='complete', evidence=<verified Worker result>)."
            expected |= {"get_goal", "update_goal"}
    else:
        raise ValueError(case)
    platform_tools = expected - {"fact_worker", "get_goal", "update_goal"}
    definition["tools"] = [{"name": name} for name in sorted(platform_tools) if not name.startswith("mcp__")]
    definition["description"] = task
    definition["workflow"] = task + "\nUse actual tool calls; finish with final_answer after the required work is verified."
    return expected


def verify_case(case: str, workspace: Path, records: list[dict], result) -> dict:
    def completed(name):
        return [item for item in records if item["tool_name"] == name and item["status"] == "completed"]

    proof = {}
    events_file = workspace / "mcp-events.jsonl"
    if events_file.exists():
        events = [json.loads(line) for line in events_file.read_text().splitlines()]
        pids = {item["pid"] for item in events}
        assert pids and not any(psutil.pid_exists(pid) for pid in pids)
        proof["mcp_processes_released"] = True
        proof["mcp_server_count"] = len(pids)
        if case == "mcp":
            assert any(item["event"] == "lookup" and item["query"] == "ticket08-live-mcp" for item in events)
        if case == "mcp_nested":
            assert any(item["event"] == "lookup" and item["query"] == "ticket08-nested-query" for item in events)
        if case == "parallel_workers":
            assert len(pids) >= 2
            assert {item["query"] for item in events if item["event"] == "lookup"} >= {"worker-one", "worker-two"}
    if case == "mcp_error":
        assert any(item["tool_name"] == "mcp__facts__fail_lookup" and item["status"] == "error" for item in records)
    if case in {"goal", "goal_worker"}:
        assert result.goal and result.goal["status"] == "complete" and result.goal["evidence"]
        proof["goal"] = dict(result.goal)
    if case == "skill":
        assert any("SKILL-ACTIVE-5832" in str(item["output"]) for item in completed("skill"))
        assert "SKILL-ACTIVE-5832" in str(result.output)
    if case == "skill_proposal":
        value = json.loads(completed("skill_manage")[-1]["output"])
        assert value["ok"] is True
        assert json.loads((Path(value["proposal_path"]) / "proposal.json").read_text())["status"] == "proposal"
        assert not (workspace / "skills" / "future-review").exists()
    if case == "memory":
        assert {item["input"].get("scope", "app") for item in completed("memory")} >= {"app", "project"}
        assert all(json.loads(item["output"])["ok"] for item in completed("memory") + completed("session_search"))
    if case == "context":
        created = completed("mcp__facts__context_payload")[-1]
        ref = re.search(r"\[ContextRef (ctx_[a-zA-Z0-9]+)", created["output"]).group(1)
        assert "PLATFORM-CTX-8426" not in created["output"]
        assert any(item["input"]["ref"] == ref and "PLATFORM-CTX-8426" in item["output"] for item in completed("loom_retrieve_context"))
        assert "PLATFORM-CTX-8426" in str(result.output)
        proof["context_ref"] = ref
    if case in {"outline_python", "outline_json", "ast", "lsp_symbols", "lsp_definition", "lsp_references", "lsp_workspace"}:
        marker = "fixture_name" if case == "outline_json" else "compute_total"
        assert any(marker in str(item.get("output")) for item in records if item["status"] == "completed" and item["tool_name"] != "final_answer")
    if case == "lsp_hover":
        assert any("source: LSP/" in str(item["output"]) for item in completed("lsp_hover")), "Real language server did not return hover data"
    if case.startswith("markdown_"):
        content = (workspace / "report.md").read_text()
        markers = {
            "markdown_structured": ("# Ticket 08", "## Result", "Validated structured output."),
            "markdown_raw": ("# Ticket 08\n", "Validated raw output."),
            "markdown_append": ("Existing content.", "## Appendix", "Validated appended output."),
        }[case]
        assert all(marker in content for marker in markers), content
        proof["artifact"] = str(workspace / "report.md")
    return proof


def run_case(case: str, workspace: Path, *, model_type: str | None = None) -> dict:
    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    dump(workspace / "attempt.json", {
        "case": case, "revision": revision, "dirty": dirty,
        "model_type": model_type or "configured-default", "status": "started",
    })
    config = workspace / "config"
    config.mkdir(mode=0o700)
    shutil.copyfile(ROOT / "config" / "llm.yaml", config / "llm.yaml")
    os.chmod(config / "llm.yaml", 0o600)
    system = {
        "runtime": {"root_dir": str(workspace / "runtime")},
        "checkpoint": {"enabled": True, "cleanup_on_success": False},
        "logging": {"console_enabled": False},
        "self_learning": {"enabled": False},
        "default_toolsets": [], "lsp_servers": {"enabled": False},
    }
    workflow = workspace / "applications" / case / "workflows" / "supervisor.yaml"
    workflow.parent.mkdir(parents=True)
    definition = {
        "name": f"validate_{case}", "agent_runtime": "smolagents",
        "runtime_options": {"max_steps": 10, "smart_summary": False, "todo_mode": "off"},
        "toolsets": [], "tools": [],
    }
    if model_type is not None:
        definition["model_type"] = model_type
    mcp_events = workspace / "mcp-events.jsonl"
    mcp_config = config / "mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"facts": {
        "command": sys.executable,
        "args": [str(ROOT / "tests/mcp_test/fixtures/stdio_server.py"), str(mcp_events)],
    }}}))
    definition["mcp_servers"] = str(mcp_config)
    expected = configure_case(case, workspace, workflow, system, definition)
    (config / "system.yaml").write_text(yaml.safe_dump(system))
    workflow.write_text(yaml.safe_dump(definition))

    from agentloom.application.runner import execute_app
    from agentloom.configuration.config import bind_config, load_project_config

    started_at = datetime.now(UTC).isoformat()
    with bind_config(load_project_config(workspace)):
        result = execute_app(workflow, file_logging=True)
    records = tool_records(workspace / "runtime")
    dump(workspace / "tool-records.json", records)
    observed = {item["tool_name"] for item in records if item["status"] == "completed"}
    assert expected <= observed, f"Required successful tools missing: {sorted(expected - observed)}"
    proof = verify_case(case, workspace, records, result)
    report = {
        "case": case, "status": "passed", "provider": "real", "runtime": "smolagents",
        "revision": revision, "dirty": dirty,
        "model_type": model_type or "configured-default",
        "started_at": started_at, "ended_at": datetime.now(UTC).isoformat(),
        "task_id": result.run.task_id, "run_id": result.run.run_id,
        "manifest": str(result.run.manifest_path), "output": result.output,
        "completed_tools": sorted(observed),
        **proof,
    }
    dump(workspace / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model-type", help="Use an existing llm.yaml profile for cross-provider validation")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if workspace.exists():
        parser.error("Use a new workspace; existing evidence must not be overwritten")
    try:
        report = run_case(args.case, workspace, model_type=args.model_type)
    except Exception as error:
        if workspace.is_dir():
            dump(workspace / "failure.json", {
                "case": args.case, "status": "failed", "error_type": type(error).__name__,
                "ended_at": datetime.now(UTC).isoformat(),
            })
        raise
    print(json.dumps({"case": report["case"], "status": report["status"], "report": str(args.workspace / "report.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
