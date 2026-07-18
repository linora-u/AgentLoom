from __future__ import annotations

from pathlib import Path

import pytest

from src.lib.smolagents.skills.parser import SkillMetadata
from src.lib.smolagents.skills.skills import SkillsManager

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_skill(root: Path, frontmatter_line: str = "") -> Path:
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


@pytest.mark.parametrize(
    "declaration",
    [
        "hooks: {}",
        "hooks: null",
        "enable-hooks: true",
    ],
)
def test_skill_frontmatter_rejects_hook_ownership(
    tmp_path: Path,
    declaration: str,
) -> None:
    manager = SkillsManager()

    with pytest.raises(ValueError, match="Hook|hooks"):
        manager.load_skill_metadata(str(_write_skill(tmp_path, declaration)))


@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_skill_enable_hooks_option_is_a_migration_error(
    tmp_path: Path,
    enabled: bool,
) -> None:
    manager = SkillsManager()
    skill_file = _write_skill(tmp_path)

    with pytest.raises(ValueError, match="skills.enable-hooks is not supported"):
        manager.load_skill_metadata(str(skill_file), enable_hooks=enabled)


def test_skill_policy_fields_reject_enable_hooks_even_without_runtime_value(
    tmp_path: Path,
) -> None:
    manager = SkillsManager()

    with pytest.raises(ValueError, match="skills.enable-hooks is not supported"):
        manager.load_skill_metadata(
            str(_write_skill(tmp_path)),
            policy_fields={"enable_hooks"},
        )


def test_skill_runtime_policy_remains_scoped_to_explicit_script_execution(
    tmp_path: Path,
) -> None:
    manager = SkillsManager()
    skill = manager.load_skill_metadata(
        str(_write_skill(tmp_path)),
        allow_scripts=False,
        allow_network=False,
    )

    assert isinstance(skill.metadata, SkillMetadata)
    assert skill.metadata.allow_scripts is False
    assert skill.metadata.allow_network is False
    assert not hasattr(skill.metadata, "hooks")
    assert not hasattr(skill.metadata, "enable_hooks")
    assert not hasattr(manager, "build_hook_handlers")


def test_repository_demo_skills_do_not_reintroduce_hook_frontmatter() -> None:
    manager = SkillsManager()
    demo_root = _PROJECT_ROOT / "applications" / "test_demo" / "skills"

    loaded = [
        manager.load_skill_metadata(str(path))
        for path in sorted(demo_root.glob("*/skill.md"))
    ]

    assert [skill.metadata.name for skill in loaded] == ["demo-skill", "demo-skill-2"]
