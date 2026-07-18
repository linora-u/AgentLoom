from pathlib import Path

from src.lib.smolagents.hooks import HookPlan, HookRun, wrap_in_system_reminder


def test_system_reminder_wraps_once_and_ignores_blank_text() -> None:
    wrapped = wrap_in_system_reminder("Check the current plan")

    assert wrapped == "<system-reminder>\nCheck the current plan\n</system-reminder>"
    assert wrap_in_system_reminder(wrapped) == wrapped
    assert wrap_in_system_reminder("   ") == ""


def test_hook_run_owns_step_number_and_effect_queue() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")

    assert run.step_number == 0
    run.step_number = 42
    assert run.step_number == 42
    assert run.consume_pending_agent_context() == []


def test_prompt_templates_document_system_reminders() -> None:
    root = Path(__file__).parents[2]
    prompt_files = [
        root / "src/lib/smolagents/prompts/toolcalling_agent.example.yaml",
        root / "src/lib/smolagents/prompts/anthropic/toolcalling_agent.example.yaml",
        root / "src/lib/smolagents/prompts/openai/toolcalling_agent.example.yaml",
        root / "src/lib/smolagents/prompts/gemini/toolcalling_agent.example.yaml",
    ]

    assert prompt_files
    for path in prompt_files:
        content = path.read_text(encoding="utf-8")
        assert "# System Reminders" in content
        assert "<system-reminder>" in content
