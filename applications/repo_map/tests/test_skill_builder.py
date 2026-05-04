"""
Tests for repo_map skill workspace preparation + validation.

All tests are pure Python (no real LLM calls).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

# Ensure repo root on path. parents[3] = AgentLoom/ (tests → repo_map → applications → AgentLoom)
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from applications.repo_map.agent_tools import pipeline_agent_tools as pat
from applications.repo_map.agent_tools.markdown_tool import generate_markdown_map
from applications.repo_map.agent_tools.scan_rank_tool import scan_and_rank


FIXTURE_PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


def _copy_fixture_project(tmp_path: Path) -> Path:
    dst = tmp_path / "sample_project"
    shutil.copytree(FIXTURE_PROJECT, dst)
    return dst


def _prepare_repo_map_outputs(project: Path) -> Path:
    output_dir = project / ".repo_map"
    scan_and_rank(
        project_path=str(project),
        output_dir=str(output_dir),
        incremental=True,
    )
    generate_markdown_map(output_dir=str(output_dir))
    return output_dir


def _load_skill_context(output_dir: Path) -> dict:
    context_path = output_dir / "data" / "skill_build_context.json"
    return json.loads(context_path.read_text(encoding="utf-8"))


def _write_fake_skill_markdown(output_dir: Path) -> Path:
    context = _load_skill_context(output_dir)
    skill_root = Path(context["skill_root"])
    skill_root.mkdir(parents=True, exist_ok=True)

    skill_md = f"""---
name: {context["skill_name"]}
description: Use when reading or changing code under sample_project and you need mapped repo_map context quickly.
---

# Sample Project Repo Map Navigator

Read `references/manifest.jsonl` first. Then resolve docs with `scripts/resolve_repo_map_docs.py`.
Always read `references/repo_map/index.md` before directory details.
For cross-module analysis, read `references/repo_map/dependencies.md`.
"""
    (skill_root / "SKILL.md").write_text(skill_md, encoding="utf-8")

    examples_dir = skill_root / "assets" / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    (examples_dir / "resolve-by-source-path.md").write_text(
        "# Resolve By Source Path\n\nUse the resolver to locate index.md and analysis.md.\n",
        encoding="utf-8",
    )
    return skill_root


def test_prepare_workspace_creates_expected_tree(tmp_path):
    project = _copy_fixture_project(tmp_path)
    output_dir = _prepare_repo_map_outputs(project)

    summary = pat.prepare_repo_map_skill_workspace(str(output_dir))
    context = _load_skill_context(output_dir)
    skill_root = Path(context["skill_root"])

    assert "Skill workspace prepared:" in summary
    assert skill_root.exists()
    assert context["skill_name"] == "sample-project-repo-map-navigator"

    assert (skill_root / "references" / "repo_map" / "index.md").exists()
    assert (skill_root / "references" / "repo_map" / "dependencies.md").exists()
    assert (skill_root / "references" / "manifest.jsonl").exists()
    assert (skill_root / "scripts" / "resolve_repo_map_docs.py").exists()
    assert (skill_root / "assets" / "examples").exists()

    progress = json.loads((output_dir / "data" / "analysis_progress.json").read_text())
    manifest_lines = [
        line.strip()
        for line in (skill_root / "references" / "manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(manifest_lines) == len(progress)


def test_resolver_script_supports_exact_and_fallback(tmp_path):
    project = _copy_fixture_project(tmp_path)
    output_dir = _prepare_repo_map_outputs(project)
    pat.prepare_repo_map_skill_workspace(str(output_dir))
    context = _load_skill_context(output_dir)
    skill_root = Path(context["skill_root"])

    script_path = skill_root / "scripts" / "resolve_repo_map_docs.py"
    spec = importlib.util.spec_from_file_location("resolver", str(script_path))
    assert spec is not None and spec.loader is not None
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)

    exact = resolver.resolve_repo_map_docs(
        source_path=str(project / "api" / "routes.py"),
        source_root=str(project),
        repo_map_ref_root=str(skill_root / "references" / "repo_map"),
    )
    assert exact[0]["dir_path"] == "api"
    assert exact[0]["index_path"].endswith("api/index.md")

    fallback = resolver.resolve_repo_map_docs(
        source_path=str(project / "new_module" / "x.py"),
        source_root=str(project),
        repo_map_ref_root=str(skill_root / "references" / "repo_map"),
    )
    assert fallback
    assert fallback[-1]["dir_path"] == "(root)"
    assert fallback[-1]["index_path"].endswith("repo_map/index.md")


def test_validate_skill_checks_frontmatter_and_examples(tmp_path):
    project = _copy_fixture_project(tmp_path)
    output_dir = _prepare_repo_map_outputs(project)
    pat.prepare_repo_map_skill_workspace(str(output_dir))

    skill_root = _write_fake_skill_markdown(output_dir)
    summary = pat.validate_repo_map_skill(str(output_dir))
    assert "Skill validation passed:" in summary

    invalid_md = """---
name: invalid
description: Use when invalid.
extra: forbidden
---
"""
    (skill_root / "SKILL.md").write_text(invalid_md, encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        pat.validate_repo_map_skill(str(output_dir))


def test_analysis_loop_prepare_validate_integration(tmp_path, monkeypatch):
    project = _copy_fixture_project(tmp_path)
    output_dir = _prepare_repo_map_outputs(project)

    # Mock create_agent_as_tool to return a simple callable (no LLM needed)
    # Do NOT set .batch — hasattr() check in run_analysis_loop will use the serial fallback
    def _mock_tool(**kwargs):
        return f"# 架构分析: {kwargs['dir_path']}\n\nmock"
    _mock_tool.__name__ = "mock_dir_analysis"
    monkeypatch.setattr(
        "src.lib.smolagents.agent.yaml_agent_factory.YamlAgentFactory.create_agent_as_tool",
        lambda *args, **kwargs: _mock_tool,
    )

    loop_summary = pat.run_analysis_loop(str(output_dir))
    prep_summary = pat.prepare_repo_map_skill_workspace(str(output_dir))
    _write_fake_skill_markdown(output_dir)
    validate_summary = pat.validate_repo_map_skill(str(output_dir))
    final_summary = pat.get_analysis_summary(str(output_dir))

    assert "Analysis loop complete" in loop_summary
    assert "Skill workspace prepared:" in prep_summary
    assert "Skill validation passed:" in validate_summary
    assert "Repo Map Generation Summary" in final_summary
