#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.skills.skills import SkillsManager


APP_ROOT = REPO_ROOT / "applications" / "skill_github_probe"
CONFIG_DIR = APP_ROOT / "config"
SYSTEM_CONFIG_PATH = CONFIG_DIR / "system.yaml"
RUNTIME_DIR = APP_ROOT / ".runtime"
REPORT_DIR = APP_ROOT / "reports"
REPORT_PATH = REPORT_DIR / "skill_validation_report.md"


@dataclass(frozen=True)
class Target:
    label: str
    category: str
    repo: str
    skill_path: str
    ref: str | None = None
    load_mode: str = "on-demand"
    commands: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Target":
        return cls(
            label=str(data["label"]),
            category=str(data.get("category", "uncategorized")),
            repo=str(data["repo"]),
            ref=str(data["ref"]) if data.get("ref") else None,
            skill_path=str(data["skill-path"]),
            load_mode=str(data.get("load-mode", "on-demand")),
            commands=list(data.get("commands") or []),
        )


def spec_get(spec: dict[str, Any], key: str, default: Any = None) -> Any:
    return spec.get(key, spec.get(key.replace("-", "_"), default))


def load_targets(path: Path = SYSTEM_CONFIG_PATH) -> list[Target]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    probe_config = data.get("skill_github_probe") or {}
    if not isinstance(probe_config, dict):
        raise ValueError(f"{path} must contain a 'skill_github_probe' mapping")
    items = probe_config.get("targets")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path} must contain a non-empty 'skill_github_probe.targets' list")
    return [Target.from_dict(item) for item in items]


def run(cmd: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def repo_dir_for(repo: str) -> Path:
    return RUNTIME_DIR / "repos" / repo.removesuffix(".git").rsplit("/", 1)[-1]


def checkout_ref(repo_dir: Path, ref: str | None) -> None:
    if not ref:
        return
    completed = run(["git", "checkout", "--detach", ref], cwd=repo_dir)
    if completed.returncode == 0:
        return
    fetched = run(["git", "fetch", "--depth", "1", "origin", ref], cwd=repo_dir, timeout=180)
    if fetched.returncode != 0:
        raise RuntimeError(f"git fetch failed for ref {ref}: {fetched.stderr.strip()}")
    checked_out = run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=repo_dir)
    if checked_out.returncode != 0:
        raise RuntimeError(f"git checkout failed for ref {ref}: {checked_out.stderr.strip()}")


def clone_repo(repo: str, ref: str | None = None) -> tuple[Path, str]:
    repo_dir = repo_dir_for(repo)
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    completed = run(["git", "clone", "--depth", "1", repo, str(repo_dir)], cwd=REPO_ROOT, timeout=180)
    if completed.returncode != 0:
        raise RuntimeError(f"git clone failed for {repo}: {completed.stderr.strip()}")
    checkout_ref(repo_dir, ref)
    commit = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if commit.returncode != 0:
        raise RuntimeError(f"git rev-parse failed for {repo}: {commit.stderr.strip()}")
    return repo_dir, commit.stdout.strip()


def hash_skill(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(skill_dir.rglob("*"), key=lambda p: p.as_posix()):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(skill_dir).as_posix()
        if "/.git/" in rel or rel.startswith(".git/") or "/node_modules/" in rel:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def command_passed(result: dict[str, Any], spec: dict[str, Any]) -> tuple[bool, str, str]:
    expected = spec_get(spec, "expect-returncode")
    if expected is not None and result.get("returncode") != expected:
        reason = spec_get(spec, "diagnostic-on-failure")
        if reason:
            return True, str(reason), "diagnostic"
        return False, f"expected returncode {expected}, got {result.get('returncode')}", "fail"
    expected_stdout = spec_get(spec, "expect-stdout-contains")
    if expected_stdout and expected_stdout not in result.get("stdout_preview", ""):
        return False, f"stdout did not contain {expected_stdout!r}", "fail"
    expected_stderr = spec_get(spec, "expect-stderr-contains")
    if expected_stderr and expected_stderr not in result.get("stderr_preview", ""):
        return False, f"stderr did not contain {expected_stderr!r}", "fail"
    diagnostic_reason = spec_get(spec, "diagnostic-reason")
    if diagnostic_reason:
        return True, str(diagnostic_reason), "diagnostic"
    return True, "ok", "pass"


def validate_target(target: Target, repo_cache: dict[tuple[str, str | None], tuple[Path, str]]) -> dict[str, Any]:
    repo_key = (target.repo, target.ref)
    if repo_key not in repo_cache:
        repo_cache[repo_key] = clone_repo(target.repo, target.ref)
    repo_dir, commit = repo_cache[repo_key]
    skill_dir = repo_dir / target.skill_path

    record: dict[str, Any] = {
        "label": target.label,
        "category": target.category,
        "repo": target.repo,
        "ref": target.ref,
        "commit": commit,
        "skill_path": target.skill_path,
        "load_mode": target.load_mode,
        "skill_dir": str(skill_dir),
        "commands": [],
    }

    try:
        record["content_hash"] = hash_skill(skill_dir)
        manager = SkillsManager(logger=None, hook_manager=HookManager())
        loaded = manager.load_skills_from_directory(str(skill_dir), load_mode=target.load_mode)
        record["loaded"] = loaded
        if not loaded:
            record.update({"status": "fail", "root_cause": "skill did not load"})
            return record

        skill_name = loaded[0]
        record["skill_name"] = skill_name
        deps_before = manager.check_skill_dependencies(skill_name)
        record["dependencies_before"] = deps_before

        command_failures: list[str] = []
        diagnostics: list[str] = []
        for spec in target.commands:
            result = manager.run_skill_script(
                skill_name,
                str(spec["command"]),
                timeout=int(spec_get(spec, "timeout", 60)),
            )
            ok, reason, outcome = command_passed(result, spec)
            record["commands"].append(
                {
                    "name": spec_get(spec, "name"),
                    "command": result["command"],
                    "returncode": result.get("returncode"),
                    "blocked": result.get("blocked"),
                    "timed_out": result.get("timed_out"),
                    "audit_dir": result.get("audit_dir"),
                    "passed": ok,
                    "outcome": outcome,
                    "reason": reason,
                }
            )
            if not ok:
                command_failures.append(f"{spec_get(spec, 'name')}: {reason}")
            elif outcome == "diagnostic":
                diagnostics.append(f"{spec_get(spec, 'name')}: {reason}")

        record["dependencies_after"] = manager.check_skill_dependencies(skill_name)
        if command_failures:
            record.update({"status": "fail", "root_cause": "; ".join(command_failures)})
        elif diagnostics:
            record.update({"status": "diagnostic", "root_cause": "; ".join(diagnostics)})
        else:
            record.update({"status": "pass", "root_cause": "ok"})
    except Exception as exc:
        record.update({"status": "fail", "root_cause": str(exc)})
    return record


def write_report(records: list[dict[str, Any]]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Skill GitHub Probe Validation Report",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        "- Runtime: AgentLoom `SkillsManager` loading + `run_skill_script` execution.",
        f"- Target config: `{SYSTEM_CONFIG_PATH.relative_to(REPO_ROOT).as_posix()}#skill_github_probe.targets`",
        "- Status meaning: `pass` means the skill loaded and expected commands succeeded; `diagnostic` means the skill loaded and missing credentials/local dependencies were diagnosed as expected; `fail` means unexpected behavior.",
        "",
        "## Summary",
        "",
        "| Target | Category | Status | Commit | Skill | Root cause |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        lines.append(
            "| {label} | {category} | {status} | `{commit}` | `{skill}` | {cause} |".format(
                label=record["label"],
                category=record.get("category", ""),
                status=record["status"],
                commit=record.get("commit", "")[:12],
                skill=record.get("skill_name", "(not loaded)"),
                cause=str(record.get("root_cause", "")).replace("|", "\\|"),
            )
        )

    lines.extend(["", "## Details", ""])
    for record in records:
        lines.extend(
            [
                f"### {record['label']}",
                "",
                f"- Category: {record.get('category', '')}",
                f"- Source URL: {record['repo']}",
                f"- Config ref: `{record.get('ref') or ''}`",
                f"- Commit SHA: `{record.get('commit', '')}`",
                f"- Skill path: `{record['skill_path']}`",
                f"- Content hash: `{record.get('content_hash', '')}`",
                f"- Load mode: `{record['load_mode']}`",
                f"- Loaded skill: `{record.get('skill_name', '(not loaded)')}`",
                f"- Status: **{record['status']}**",
                f"- Root cause: {record.get('root_cause', '')}",
                "",
            ]
        )
        deps_before = record.get("dependencies_before")
        deps_after = record.get("dependencies_after")
        if deps_before is not None:
            lines.append(f"- Dependencies before: `{json.dumps(deps_before, ensure_ascii=False)}`")
        if deps_after is not None:
            lines.append(f"- Dependencies after: `{json.dumps(deps_after, ensure_ascii=False)}`")
        if record.get("commands"):
            lines.extend(["", "Commands:"])
            for command in record["commands"]:
                lines.append(
                    "- `{name}`: `{command}` -> returncode={returncode}, passed={passed}, reason={reason}, audit={audit}".format(
                        name=command["name"],
                        command=command["command"],
                        returncode=command["returncode"],
                        passed=command["passed"],
                        reason=f"{command['outcome']}: {command['reason']}",
                        audit=command["audit_dir"],
                    )
                )
        lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    targets = load_targets()
    repo_cache: dict[tuple[str, str | None], tuple[Path, str]] = {}
    records = [validate_target(target, repo_cache) for target in targets]
    write_report(records)
    print(str(REPORT_PATH))
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 1 if any(record["status"] == "fail" for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
