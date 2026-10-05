import pytest

from news_agent.skill_reference import read_news_reference


def test_reader_returns_a_real_skill_reference():
    value = read_news_reference("robotics.md")
    assert value.startswith("# 机器人与核心部件")
    assert "量产" in value


@pytest.mark.parametrize("filename", ["../agent/精筛.md", "/etc/passwd", "robotics.md/../", "absent.md"])
def test_reader_rejects_non_reference_paths(filename: str):
    with pytest.raises(ValueError):
        read_news_reference(filename)
