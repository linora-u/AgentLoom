from pathlib import Path

import pytest

from src.lib.smolagents.skills.catalog import SkillCatalog, SkillSource


def _write_skill(root: Path, frontmatter_line: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    skill_file = root / "SKILL.md"
    skill_file.write_text(
        "\n".join(
            [
                "---",
                "name: sample-skill",
                'description: "Skill and Hook separation fixture"',
                *([frontmatter_line] if frontmatter_line else []),
                "---",
                "fixture",
            ]
        ),
        encoding="utf-8",
    )
    return skill_file


@pytest.mark.parametrize("declaration", ["hooks: {}", "hooks: null", "enable-hooks: true"])
def test_skill_frontmatter_rejects_hook_ownership(tmp_path: Path, declaration: str) -> None:
    manifest = _write_skill(tmp_path / "sample", declaration)

    with pytest.raises(ValueError, match="Hook|hooks"):
        SkillCatalog.discover([SkillSource(path=manifest, scope="project")])


def test_repository_demo_skills_remain_discoverable() -> None:
    project_root = Path(__file__).resolve().parents[2]
    catalog = SkillCatalog.discover(
        [SkillSource(path=project_root / "applications" / "test_demo" / "skills", scope="application")]
    )

    assert [skill.name for skill in catalog.summaries()] == ["demo-skill", "demo-skill-2"]
