"""快速验证重构正确性：不启动 LLM，只检查导入、yaml 路径、文件存在、schema 字段。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

errors = []

# 1. pipeline_agent_tools 导入
_DIR_ANALYSIS_YAML = None
try:
    from applications.repo_map.agent_tools.pipeline_agent_tools import (
        run_analysis_loop, get_analysis_summary
    )
    import applications.repo_map.agent_tools.pipeline_agent_tools as _pat
    _DIR_ANALYSIS_YAML = _pat._DIR_ANALYSIS_YAML
    print(f"[OK] pipeline_agent_tools imports fine")
    print(f"     _DIR_ANALYSIS_YAML = {_DIR_ANALYSIS_YAML}")
except Exception as e:
    errors.append(f"[FAIL] pipeline_agent_tools import: {e}")

# 2. yaml 路径指向新文件
if _DIR_ANALYSIS_YAML is None:
    errors.append("[FAIL] _DIR_ANALYSIS_YAML not set (import failed)")
yaml_path = Path(_DIR_ANALYSIS_YAML or "UNKNOWN")
if "dir_architecture_analysis" in str(yaml_path) and "step3" not in str(yaml_path):
    print(f"[OK] yaml path uses new name: {yaml_path.name}")
else:
    errors.append(f"[FAIL] yaml path still uses old name: {yaml_path}")

# 3. 新 yaml 文件存在
new_yaml = Path("applications/repo_map/workflows/worker_agents/dir_architecture_analysis.yaml")
if new_yaml.exists():
    print(f"[OK] dir_architecture_analysis.yaml exists")
else:
    errors.append(f"[FAIL] dir_architecture_analysis.yaml NOT FOUND")

# 4. 旧 yaml 已删除
for old in ["step1_scan_extract_rank.yaml", "step2_generate_markdown.yaml", "step3_architecture_analysis.yaml"]:
    p = Path(f"applications/repo_map/workflows/worker_agents/{old}")
    if not p.exists():
        print(f"[OK] {old} deleted")
    else:
        errors.append(f"[FAIL] {old} still exists (should be deleted)")

# 5. dir_architecture_analysis.yaml 内容正确
try:
    import yaml
    content = yaml.safe_load(new_yaml.read_text())
    name = content.get("name", "")
    schema = content.get("agent_function_schema", {})
    inputs = schema.get("inputs", {})
    assert name == "dir_architecture_analysis", f"name={name}"
    assert "dir_path" in inputs, f"missing dir_path, got {list(inputs.keys())}"
    assert "output_dir" in inputs, f"missing output_dir, got {list(inputs.keys())}"
    assert "output" in schema, "missing output field"
    print(f"[OK] yaml schema: name={name}, inputs={list(inputs.keys())}")
except Exception as e:
    errors.append(f"[FAIL] yaml schema check: {e}")

# 6. repo_map_agent.yaml 不再有 worker step1/step2
agent_yaml = Path("applications/repo_map/workflows/repo_map_agent.yaml")
if agent_yaml.exists():
    content_str = agent_yaml.read_text()
    if "step1_scan_extract_rank" not in content_str and "step2_generate_markdown" not in content_str:
        print("[OK] repo_map_agent.yaml has no step1/step2 references")
    else:
        errors.append("[FAIL] repo_map_agent.yaml still references step1 or step2")
    if "dir_architecture_analysis" in content_str:
        print("[OK] repo_map_agent.yaml references dir_architecture_analysis")
    else:
        errors.append("[FAIL] repo_map_agent.yaml missing dir_architecture_analysis reference")

# 7. repo_map_app.py 直接调用 scan_and_rank
app_py = Path("applications/repo_map/repo_map_app.py")
content_str = app_py.read_text()
if "scan_and_rank" in content_str and "generate_markdown_map" in content_str:
    print("[OK] repo_map_app.py directly calls scan_and_rank + generate_markdown_map")
else:
    errors.append("[FAIL] repo_map_app.py missing direct calls to scan/markdown tools")

# 8. 测试文件检查
scan_test = Path("applications/repo_map/tests/test_scan_rank_tool.py")
demo_script = Path("applications/repo_map/tests/run_demo.py")
old_test = Path("applications/repo_map/tests/test_pipeline_agent_tools.py")
old_llm_test = Path("applications/repo_map/tests/test_llm_integration.py")
if scan_test.exists():
    print("[OK] test_scan_rank_tool.py exists")
else:
    errors.append("[FAIL] test_scan_rank_tool.py missing")
if demo_script.exists():
    print("[OK] run_demo.py exists")
else:
    errors.append("[FAIL] run_demo.py missing")
if not old_test.exists():
    print("[OK] test_pipeline_agent_tools.py deleted")
else:
    errors.append("[FAIL] test_pipeline_agent_tools.py still exists (should be deleted)")
if not old_llm_test.exists():
    print("[OK] test_llm_integration.py deleted (replaced by run_demo.py)")
else:
    errors.append("[FAIL] test_llm_integration.py still exists (should be replaced by run_demo.py)")

print()
if errors:
    print(f"FAILED ({len(errors)} issues):")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print(f"ALL {8} CHECKS PASSED")
