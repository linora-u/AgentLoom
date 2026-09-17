"""Generated files must execute values, keyword-only branches and exceptions."""
import json
import subprocess
import sys
from pathlib import Path

from applications.unit_test_studio.agent_tools.unit_test_tools import (
    build_pytest_template, get_function_context, plan_test_scenarios, upsert_pytest_file,
)


def test_generated_tests_execute_python_values_and_expected_exceptions(tmp_path):
    target = tmp_path / "subject.py"
    target.write_text('''def normalize_message(text, *, max_len=8, strict=False):
    if text is None:
        raise TypeError("text cannot be None")
    if max_len <= 0:
        raise ValueError("positive length required")
    if not text:
        return None
    return {"value": text.strip()[:max_len], "strict": strict}
''')
    context = get_function_context(str(tmp_path), "subject.py", "normalize_message")
    scenario = plan_test_scenarios(context, "normalize_message")
    cases = json.loads(scenario)["cases"]
    assert any(case.get("raises") == "TypeError" for case in cases)
    assert any(case.get("raises") == "ValueError" for case in cases)
    assert any(case["input"].get("strict") is True for case in cases)
    assert any(case.get("expected", "missing") is None for case in cases)
    content = build_pytest_template("subject.py", "normalize_message", scenario, "generated")
    generated = json.loads(upsert_pytest_file(str(tmp_path), "subject.py", "normalize_message", content, "generated"))
    result = subprocess.run([sys.executable, "-m", "pytest", generated["file_path"], "-q"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    # Prove the generated assertions reject a real behavior regression.
    target.write_text(target.read_text().replace('raise TypeError("text cannot be None")', 'return "wrong"'))
    result = subprocess.run([sys.executable, "-m", "pytest", generated["file_path"], "-q"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "DID NOT RAISE" in result.stdout
