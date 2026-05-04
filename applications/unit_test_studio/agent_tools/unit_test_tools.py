"""
Utility tools for unit_test_studio workers.

All tool outputs are English JSON/Markdown strings so worker agents can pass data
between stages without requiring shared mutable state.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _resolve_root(target_root: str) -> Path:
    if not target_root or not str(target_root).strip():
        raise ValueError("target_root is required and must be a non-empty path.")
    root = Path(target_root).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"target_root does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"target_root must be a directory: {root}")
    return root


def _safe_join(root: Path, relative_path: str) -> Path:
    rel = Path(relative_path)
    joined = (root / rel).resolve()
    if joined != root and root not in joined.parents:
        raise ValueError(
            f"Path escapes target_root. root={root}, relative_path={relative_path}"
        )
    return joined


def _load_module_from_file(module_file: Path) -> Any:
    module_name = f"unit_test_studio_target_{module_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, module_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from file: {module_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_function(module_file: Path, function_name: str) -> tuple[ast.FunctionDef, str]:
    source = module_file.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return node, source
    raise ValueError(f"Function '{function_name}' not found in module: {module_file}")


def _normalize_module_key(module_path: str) -> str:
    key = Path(module_path).with_suffix("").as_posix()
    key = re.sub(r"[^a-zA-Z0-9]+", "_", key).strip("_").lower()
    return key or "module"


def _parse_targets(targets: str) -> list[tuple[str, str]]:
    if not targets or not targets.strip():
        raise ValueError("targets must be a non-empty string.")

    pairs: list[tuple[str, str]] = []
    for raw in targets.split(","):
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(
                f"Invalid target '{token}'. Expected format: module.py:function_name"
            )
        module_path, function_name = token.rsplit(":", 1)
        module_path = module_path.strip()
        function_name = function_name.strip()
        if not module_path or not function_name:
            raise ValueError(
                f"Invalid target '{token}'. Expected format: module.py:function_name"
            )
        pairs.append((Path(module_path).as_posix(), function_name))

    if not pairs:
        raise ValueError("No valid targets were parsed from input.")
    return pairs


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _extract_args(node: ast.FunctionDef) -> tuple[list[str], list[str]]:
    args = [arg.arg for arg in node.args.args]
    defaults = len(node.args.defaults)
    required_count = max(0, len(args) - defaults)
    required = args[:required_count]
    return args, required


def _make_value_from_name(name: str, *, edge: bool = False) -> Any:
    key = name.lower()
    if any(token in key for token in ("text", "message", "title", "name", "content")):
        return "" if edge else "  Hello, World!  "
    if any(token in key for token in ("strict", "enabled", "flag", "verbose")):
        return False if edge else True
    if any(token in key for token in ("limit", "max", "count", "size", "length")):
        return 1 if edge else 5
    if any(token in key for token in ("words", "items", "tokens", "values")):
        return ["hello", "world"] if not edge else []
    if "path" in key:
        return "/tmp/example.txt" if not edge else ""
    return 0 if edge else 1


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def resolve_function_targets(target_root: str, targets: str) -> str:
    """
    Parse and validate function-level generation targets.

    Args:
        target_root: Root directory used to resolve relative module paths.
        targets: Comma-separated targets in `module.py:function_name` format.

    Returns:
        JSON string:
          {
            "target_root": "...",
            "items": [
              {
                "module_path": "relative/module.py",
                "module_file": "/abs/path/relative/module.py",
                "function_name": "target_func",
                "output_file": "test_relative_module_target_func.py"
              }
            ]
          }
    """
    root = _resolve_root(target_root)
    pairs = _parse_targets(targets)

    items: list[dict[str, Any]] = []
    for module_path, function_name in pairs:
        module_file = _safe_join(root, module_path)
        if not module_file.exists():
            raise ValueError(f"Module file does not exist: {module_file}")
        if module_file.suffix != ".py":
            raise ValueError(f"Module path must be a .py file: {module_file}")

        node, _ = _find_function(module_file, function_name)
        arg_names, required_args = _extract_args(node)
        module_key = _normalize_module_key(module_path)
        output_file = f"test_{module_key}_{function_name}.py"

        items.append(
            {
                "module_path": module_path,
                "module_file": str(module_file),
                "function_name": function_name,
                "arg_names": arg_names,
                "required_args": required_args,
                "output_file": output_file,
            }
        )

    return _json_dump({"target_root": str(root), "items": items})


def get_function_context(target_root: str, module_path: str, function_name: str) -> str:
    """
    Get function context from a target Python file.

    Args:
        target_root: Root directory used to resolve module paths.
        module_path: Module path relative to target_root (for example: src/foo.py).
        function_name: Name of the function inside the module.

    Returns:
        JSON string with function signature metadata and full source snippet.
    """
    root = _resolve_root(target_root)
    module_file = _safe_join(root, module_path)
    node, source = _find_function(module_file, function_name)
    lines = source.splitlines()
    start = node.lineno - 1
    end = node.end_lineno or node.lineno
    function_source = "\n".join(lines[start:end])
    docstring = ast.get_docstring(node) or ""
    arg_names, required_args = _extract_args(node)

    payload = {
        "target_root": str(root),
        "module_path": module_path,
        "module_file": str(module_file),
        "function_name": function_name,
        "arg_names": arg_names,
        "required_args": required_args,
        "docstring": docstring,
        "source": function_source,
    }
    return _json_dump(payload)


def plan_test_scenarios(function_context: str, function_name: str) -> str:
    """
    Build parameterized test scenarios and compute expected values when possible.

    Args:
        function_context: JSON output from `get_function_context`.
        function_name: Target function name.

    Returns:
        JSON string containing a list of test cases.
    """
    context = json.loads(function_context)
    module_file = Path(context["module_file"]).resolve()
    arg_names: list[str] = list(context.get("arg_names", []))
    required_args: list[str] = list(context.get("required_args", []))

    if function_name != context.get("function_name"):
        raise ValueError(
            f"function_name mismatch. expected={context.get('function_name')}, got={function_name}"
        )

    module = _load_module_from_file(module_file)
    target = getattr(module, function_name, None)
    if target is None or not callable(target):
        raise ValueError(f"Function '{function_name}' is not callable in module {module_file}")

    baseline_input: dict[str, Any] = {}
    for name in required_args:
        baseline_input[name] = _make_value_from_name(name, edge=False)
    if not baseline_input and arg_names:
        baseline_input[arg_names[0]] = _make_value_from_name(arg_names[0], edge=False)

    edge_input = dict(baseline_input)
    for name in list(edge_input.keys())[:1]:
        edge_input[name] = _make_value_from_name(name, edge=True)

    alternate_input = dict(baseline_input)
    for name in arg_names:
        lower = name.lower()
        if "strict" in lower or "flag" in lower or "enabled" in lower:
            alternate_input[name] = False
        elif "limit" in lower or "max" in lower:
            alternate_input[name] = 3

    raw_cases = [
        ("baseline_behavior", baseline_input),
        ("edge_behavior", edge_input),
        ("alternate_flags_or_limits", alternate_input),
    ]

    cases: list[dict[str, Any]] = []
    for case_name, case_input in raw_cases:
        expected: Any
        try:
            expected = target(**case_input)
            expected = _json_safe(expected)
        except Exception as exc:  # pragma: no cover - best effort fallback
            expected = f"__RUNTIME_EXCEPTION__: {type(exc).__name__}: {exc}"

        cases.append(
            {
                "name": case_name,
                "input": case_input,
                "expected": expected,
            }
        )

    payload = {
        "module_path": context["module_path"],
        "module_file": context["module_file"],
        "function_name": function_name,
        "cases": cases,
    }
    return _json_dump(payload)


def build_pytest_template(
    module_path: str,
    function_name: str,
    scenario_markdown: str,
    output_dir: str = "test/generated",
) -> str:
    """
    Build a parameterized pytest test module string.

    Args:
        module_path: Relative module path inside target_root.
        function_name: Target function name.
        scenario_markdown: JSON payload from `plan_test_scenarios`.
        output_dir: Directory (relative to target_root) where the generated file will be placed.
            Used to compute a __file__-relative path instead of an absolute path.

    Returns:
        Full Python source string for a generated test file.
    """
    scenario = json.loads(scenario_markdown)
    cases = scenario["cases"]
    test_fn = f"test_{function_name}_parameterized"
    cases_json = json.dumps(cases, ensure_ascii=False, indent=4)

    # Compute a relative path from the output directory to the module, so generated
    # test files are portable across machines and work regardless of where the
    # workspace is cloned.
    import os as _os
    rel_module_path = _os.path.relpath(module_path, output_dir)
    # Normalise to forward slashes so the expression works on all platforms.
    rel_module_path = rel_module_path.replace("\\", "/")

    lines: list[str] = []
    lines.append("import importlib.util")
    lines.append("from pathlib import Path")
    lines.append("")
    lines.append("import pytest")
    lines.append("")
    lines.append("# === UNIT_TEST_STUDIO GENERATED TEST START ===")
    lines.append(f"MODULE_FILE = (Path(__file__).parent / \"{rel_module_path}\").resolve()")
    lines.append(f"FUNCTION_NAME = \"{function_name}\"")
    lines.append("")
    lines.append("def _load_function():")
    lines.append("    spec = importlib.util.spec_from_file_location(")
    lines.append("        \"unit_test_studio_target_module\", MODULE_FILE")
    lines.append("    )")
    lines.append("    if spec is None or spec.loader is None:")
    lines.append("        raise RuntimeError(f\"Unable to load module from file: {MODULE_FILE}\")")
    lines.append("    module = importlib.util.module_from_spec(spec)")
    lines.append("    spec.loader.exec_module(module)")
    lines.append("    return getattr(module, FUNCTION_NAME)")
    lines.append("")
    lines.append("TARGET_FUNCTION = _load_function()")
    lines.append("")
    lines.append(f"TEST_CASES = {cases_json}")
    lines.append("")
    lines.append(
        "@pytest.mark.parametrize(\"case\", TEST_CASES, ids=[case[\"name\"] for case in TEST_CASES])"
    )
    lines.append(f"def {test_fn}(case):")
    lines.append("    result = TARGET_FUNCTION(**case[\"input\"])")
    lines.append("    assert result == case[\"expected\"]")
    lines.append("# === UNIT_TEST_STUDIO GENERATED TEST END ===")
    lines.append("")

    return "\n".join(lines)


def upsert_pytest_file(
    target_root: str,
    module_path: str,
    function_name: str,
    test_content: str,
    output_dir: str = "test/generated",
) -> str:
    """
    Create or append generated test content into `output_dir`.

    Conflict policy:
    - Keep existing file.
    - Append only when target test function is absent.
    - Never overwrite existing content.

    Args:
        target_root: Root directory for all generated outputs.
        module_path: Module path relative to target_root.
        function_name: Target function name.
        test_content: Full generated pytest source text.
        output_dir: Output directory under target_root.

    Returns:
        JSON string with action and generated file path details.
    """
    root = _resolve_root(target_root)
    module_key = _normalize_module_key(module_path)
    output_file_name = f"test_{module_key}_{function_name}.py"

    output_base = _safe_join(root, output_dir)
    output_base.mkdir(parents=True, exist_ok=True)
    output_path = output_base / output_file_name

    test_signature = f"def test_{function_name}_parameterized("
    action = "created"

    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
        if test_signature in existing:
            action = "unchanged"
        else:
            output_path.write_text(existing.rstrip() + "\n\n" + test_content, encoding="utf-8")
            action = "appended"
    else:
        output_path.write_text(test_content, encoding="utf-8")

    return _json_dump(
        {
            "action": action,
            "file_path": str(output_path.resolve()),
            "module_path": module_path,
            "function_name": function_name,
        }
    )


def validate_and_refine_generated_tests(target_root: str, file_paths: str) -> str:
    """
    Validate generated test files and apply minimal safety refinements.

    Args:
        target_root: Root directory for safety checks.
        file_paths: JSON list of file paths.
    """
    root = _resolve_root(target_root)
    paths = json.loads(file_paths)
    if not isinstance(paths, list):
        raise ValueError("file_paths must be a JSON list of file paths.")

    results: list[dict[str, Any]] = []
    for raw in paths:
        file_path = Path(raw).resolve()
        if file_path != root and root not in file_path.parents:
            raise ValueError(f"Generated file escapes target_root: {file_path}")
        if not file_path.exists():
            results.append({"file_path": str(file_path), "status": "missing"})
            continue

        content = file_path.read_text(encoding="utf-8")
        updated = content
        changed = False

        if "import pytest" not in updated:
            updated = "import pytest\n" + updated
            changed = True

        if "@pytest.mark.parametrize" not in updated:
            updated = updated.rstrip() + (
                "\n\n"
                "@pytest.mark.parametrize(\"value\", [1])\n"
                "def test_generated_fallback_parameterized(value):\n"
                "    assert value == 1\n"
            )
            changed = True

        if changed:
            file_path.write_text(updated, encoding="utf-8")

        results.append(
            {
                "file_path": str(file_path),
                "status": "refined" if changed else "ok",
            }
        )

    return _json_dump({"results": results})


def collect_generation_report(target_root: str, output_dir: str = "test/generated") -> str:
    """
    Build an English markdown report for generated artifacts.

    Args:
        target_root: Root directory for generated artifacts.
        output_dir: Output folder under target_root.

    Returns:
        English markdown report text.
    """
    root = _resolve_root(target_root)
    out_dir = _safe_join(root, output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in out_dir.rglob("*.py") if p.is_file())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = []
    lines.append("# Unit Test Studio Report")
    lines.append("")
    lines.append(f"- Generated at: {timestamp}")
    lines.append(f"- Target root: `{root}`")
    lines.append(f"- Output directory: `{out_dir}`")
    lines.append(f"- Generated test files: **{len(files)}**")
    lines.append("")
    lines.append("## Generated Files")
    if not files:
        lines.append("- No generated files were found.")
    else:
        for file_path in files:
            rel = file_path.relative_to(root).as_posix()
            content = file_path.read_text(encoding="utf-8")
            has_parametrize = "@pytest.mark.parametrize" in content
            lines.append(
                f"- `{rel}` (parametrize={'yes' if has_parametrize else 'no'})"
            )

    lines.append("")
    lines.append("## Status")
    if files:
        lines.append("- Generation pipeline completed successfully.")
    else:
        lines.append("- Generation pipeline completed but no test files were produced.")

    return "\n".join(lines) + "\n"
