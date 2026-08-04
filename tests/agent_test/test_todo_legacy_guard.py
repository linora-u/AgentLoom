"""Guard the intentionally breaking removal of the legacy Todo protocol."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_todo_runtime_and_prompt_contracts_are_absent() -> None:
    runtime_files = [
        PROJECT_ROOT / "src/lib/smolagents/agent/loom_mixin.py",
        PROJECT_ROOT / "src/lib/smolagents/agent/base_agent.py",
        PROJECT_ROOT / "src/tools/todo/todo_write.py",
    ]
    prompt_files = list((PROJECT_ROOT / "src/lib/smolagents/prompts").glob("**/*.example.yaml"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)
    prompts = "\n".join(path.read_text(encoding="utf-8") for path in prompt_files)

    assert "TodoSyncMixin" not in source
    assert "todos.md" not in source
    assert "_current_todos" not in source
    assert "todo_initial:" not in prompts
    assert "todo_update:" not in prompts
    assert "todo_final:" not in prompts
