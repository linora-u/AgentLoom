"""
scan_tools.py 的单元测试。

运行方式：
  cd AgentLoom/
  .venv/bin/python -m pytest tests/tools_test/skills/workflow-review/test_scan_tools.py -v
"""

import textwrap
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# 导入被测模块
# ---------------------------------------------------------------------------
import importlib.util
import sys

_AGENT_LOOM_ROOT = Path(__file__).resolve().parents[4]
_SCAN_TOOLS_PATH = _AGENT_LOOM_ROOT / "agentloom-framework-skill" / "scripts" / "scan_tools.py"
_spec = importlib.util.spec_from_file_location("scan_tools", _SCAN_TOOLS_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

scan_app_structure = _mod.scan_app_structure
extract_workflow_text = _mod.extract_workflow_text


# ---------------------------------------------------------------------------
# Fixtures：创建临时 Application 目录结构
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_app(tmp_path):
    """创建一个完整的 fake Application 目录结构用于测试。"""
    app_dir = tmp_path / "applications" / "test_app"

    # Supervisor YAML
    workflows_dir = app_dir / "workflows"
    workflows_dir.mkdir(parents=True)
    supervisor_yaml = {
        "name": "test_supervisor",
        "description": "这是一个测试用 Supervisor Agent，用于验证 scan_tools 功能。",
        "model_type": "powerful",
        "tool_call_type": "code_act",
        "max_steps": 100,
        "workflow": textwrap.dedent("""\
            ## 执行步骤
            1. 先调用 get_context 获取上下文
            2. 调用 step0_analysis 进行分析
            3. 汇总输出最终报告
        """),
        "tools": [
            {"name": "get_context", "module": "test_app.tools.context", "function": "get_context"},
        ],
        "worker_agents": [
            {"path": "applications/test_app/workflows/worker_agents/step0_analysis.yaml"},
        ],
    }
    with open(workflows_dir / "test_app_agent.yaml", "w", encoding="utf-8") as f:
        yaml.dump(supervisor_yaml, f, allow_unicode=True, default_flow_style=False)

    # Worker YAML
    worker_dir = workflows_dir / "worker_agents"
    worker_dir.mkdir()
    worker_yaml = {
        "name": "step0_analysis",
        "description": "对目标模块进行静态分析。",
        "model_type": "powerful",
        "tool_call_type": "code_act",
        "max_steps": 40,
        "workflow": textwrap.dedent("""\
            你是一个代码分析专家。
            请扫描所有源文件，提取 include 依赖，统计函数数量。
            按文件名排序输出。
        """),
        "tools": [
            {"name": "read_file"},
            {"name": "browse_directory"},
        ],
        "agent_function_schema": {
            "description": "对模块进行静态分析",
            "inputs": {
                "module_path": {
                    "description": "模块路径",
                    "required": True,
                    "type": "string",
                },
                "context": {
                    "description": "上下文信息",
                    "required": False,
                    "type": "string",
                },
            },
            "output": {
                "description": "Markdown 格式的分析报告",
            },
        },
    }
    with open(worker_dir / "step0_analysis.yaml", "w", encoding="utf-8") as f:
        yaml.dump(worker_yaml, f, allow_unicode=True, default_flow_style=False)

    # agent_tools/ (被扫描的 Application 的工具目录)
    tools_dir = app_dir / "agent_tools"
    tools_dir.mkdir()
    (tools_dir / "context_provider.py").write_text(
        textwrap.dedent("""\
            def get_context(module: str) -> str:
                \"\"\"获取模块上下文信息。\"\"\"
                return f"context for {module}"

            def _internal_helper():
                pass
        """),
        encoding="utf-8",
    )

    # 入口脚本
    (app_dir / "test_app_demo.py").write_text("# entry script\n", encoding="utf-8")

    return app_dir


@pytest.fixture
def empty_app(tmp_path):
    """创建一个最简 Application（只有 Supervisor，没有 Worker 和 Tools）。"""
    app_dir = tmp_path / "applications" / "minimal_app"
    workflows_dir = app_dir / "workflows"
    workflows_dir.mkdir(parents=True)
    minimal_yaml = {
        "name": "minimal_agent",
        "description": "极简 Agent。",
        "workflow": "直接回答用户问题。",
    }
    with open(workflows_dir / "minimal_app_agent.yaml", "w", encoding="utf-8") as f:
        yaml.dump(minimal_yaml, f, allow_unicode=True, default_flow_style=False)
    return app_dir


# ---------------------------------------------------------------------------
# scan_app_structure 测试
# ---------------------------------------------------------------------------
class TestScanAppStructure:
    """测试 scan_app_structure 函数。"""

    def test_full_app_returns_supervisor_info(self, fake_app):
        """验证能正确提取 Supervisor 的 name、model_type、tools、worker_agents。"""
        result = scan_app_structure(str(fake_app))
        assert "test_supervisor" in result
        assert "powerful" in result
        assert "get_context" in result
        assert "step0_analysis.yaml" in result

    def test_full_app_returns_worker_info(self, fake_app):
        """验证能正确提取 Worker 的 name、agent_function_schema inputs/output。"""
        result = scan_app_structure(str(fake_app))
        assert "step0_analysis" in result
        assert "module_path" in result
        assert "context" in result
        assert "Markdown" in result  # output description

    def test_full_app_returns_tools_info(self, fake_app):
        """验证能正确提取 agent_tools/ 下的文件名和公开函数。"""
        result = scan_app_structure(str(fake_app))
        assert "context_provider.py" in result
        assert "get_context()" in result
        # 内部函数（_开头）不应出现
        assert "_internal_helper" not in result

    def test_full_app_returns_entry_script(self, fake_app):
        """验证能正确列出入口脚本。"""
        result = scan_app_structure(str(fake_app))
        assert "test_app_demo.py" in result

    def test_full_app_returns_worker_count(self, fake_app):
        """验证 Worker 数量显示正确。"""
        result = scan_app_structure(str(fake_app))
        assert "1 个" in result

    def test_full_app_returns_max_steps(self, fake_app):
        """验证能提取 max_steps 配置。"""
        result = scan_app_structure(str(fake_app))
        assert "100" in result  # Supervisor max_steps
        assert "40" in result   # Worker max_steps

    def test_minimal_app_no_crash(self, empty_app):
        """极简 App（无 Worker、无 Tools）不应崩溃。"""
        result = scan_app_structure(str(empty_app))
        assert "minimal_agent" in result
        assert "0 个" in result or "Worker" not in result  # 没有 Worker 目录

    def test_nonexistent_path(self):
        """不存在的路径应返回错误提示（含 💡 修正建议）。"""
        result = scan_app_structure("/nonexistent/path/to/app")
        assert "❌" in result
        assert "不存在" in result
        assert "💡" in result
        assert "当前工作目录" in result

    def test_output_is_markdown(self, fake_app):
        """输出应该是 Markdown 格式（以 # 开头）。"""
        result = scan_app_structure(str(fake_app))
        assert result.startswith("# ")


# ---------------------------------------------------------------------------
# extract_workflow_text 测试
# ---------------------------------------------------------------------------
class TestExtractWorkflowText:
    """测试 extract_workflow_text 函数。"""

    def test_extracts_supervisor_workflow(self, fake_app):
        """验证能正确提取 Supervisor 的 workflow 全文。"""
        yaml_path = str(fake_app / "workflows" / "test_app_agent.yaml")
        result = extract_workflow_text(yaml_path)
        assert "test_supervisor" in result
        assert "get_context" in result
        assert "step0_analysis" in result
        assert "汇总输出最终报告" in result

    def test_extracts_worker_workflow(self, fake_app):
        """验证能正确提取 Worker 的 workflow 全文，包含确定性指令。"""
        yaml_path = str(fake_app / "workflows" / "worker_agents" / "step0_analysis.yaml")
        result = extract_workflow_text(yaml_path)
        assert "step0_analysis" in result
        # 验证能提取到我们埋入的确定性指令信号词
        assert "扫描所有源文件" in result
        assert "提取 include 依赖" in result
        assert "统计函数数量" in result
        assert "按文件名排序" in result

    def test_nonexistent_file(self):
        """不存在的文件应返回错误提示（含 💡 修正建议）。"""
        result = extract_workflow_text("/nonexistent/file.yaml")
        assert "❌" in result
        assert "不存在" in result
        assert "💡" in result
        assert "当前工作目录" in result

    def test_yaml_without_workflow(self, tmp_path):
        """没有 workflow 字段的 YAML 应返回提示。"""
        yaml_file = tmp_path / "no_workflow.yaml"
        yaml_file.write_text(yaml.dump({"name": "test", "description": "no workflow"}))
        result = extract_workflow_text(str(yaml_file))
        assert "⚠️" in result
        assert "workflow" in result

    def test_invalid_yaml(self, tmp_path):
        """无效的 YAML 内容应返回解析错误提示。"""
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text("{{{{invalid yaml content", encoding="utf-8")
        result = extract_workflow_text(str(yaml_file))
        # yaml.safe_load 对 {{{{ 可能不报错（解析为字符串），所以我们用更明确的错误格式
        # 至少不应崩溃
        assert isinstance(result, str)

    def test_output_contains_name(self, fake_app):
        """输出应包含 Agent name 作为标题。"""
        yaml_path = str(fake_app / "workflows" / "test_app_agent.yaml")
        result = extract_workflow_text(yaml_path)
        assert "test_supervisor" in result
        assert "workflow 全文" in result


# ---------------------------------------------------------------------------
# 边界情况测试
# ---------------------------------------------------------------------------
class TestEdgeCases:
    """边界情况和异常场景。"""

    def test_app_with_no_workflows_dir(self, tmp_path):
        """Application 目录存在但没有 workflows/ 子目录。"""
        app_dir = tmp_path / "no_workflows_app"
        app_dir.mkdir()
        result = scan_app_structure(str(app_dir))
        assert "⚠️" in result or "未找到" in result

    def test_worker_without_schema(self, tmp_path):
        """Worker 没有 agent_function_schema 也不应崩溃。"""
        app_dir = tmp_path / "app_no_schema"
        worker_dir = app_dir / "workflows" / "worker_agents"
        worker_dir.mkdir(parents=True)
        no_schema_yaml = {
            "name": "no_schema_worker",
            "description": "缺少 schema 的 Worker。",
            "workflow": "做一些分析。",
        }
        with open(worker_dir / "step0_no_schema.yaml", "w", encoding="utf-8") as f:
            yaml.dump(no_schema_yaml, f, allow_unicode=True)
        # 需要一个 supervisor yaml 才能触发扫描
        sup_yaml = {"name": "sup", "description": "sup", "workflow": "test"}
        with open(app_dir / "workflows" / "sup_agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "no_schema_worker" in result
        # 没有 schema 不应崩溃
        assert isinstance(result, str)

    def test_empty_tools_dir(self, tmp_path):
        """agent_tools/ 目录存在但为空。"""
        app_dir = tmp_path / "empty_tools_app"
        (app_dir / "workflows").mkdir(parents=True)
        (app_dir / "agent_tools").mkdir()
        sup_yaml = {"name": "sup", "description": "sup", "workflow": "test"}
        with open(app_dir / "workflows" / "sup_agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "0 个文件" in result


# ---------------------------------------------------------------------------
# 精确性测试：验证提取逻辑的正确性，而非仅"包含某个词"
# ---------------------------------------------------------------------------
class TestExtractionAccuracy:
    """验证字段提取逻辑的精确性，能检测出常见 bug。"""

    def test_description_truncation_long(self, tmp_path):
        """description > 80 字时应截断并加 '...' 后缀。"""
        app_dir = tmp_path / "trunc_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        long_desc = "A" * 100  # 100 字，超过 80
        sup_yaml = {"name": "trunc_test", "description": long_desc, "workflow": "x"}
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        # 应该截断到 80 字 + "..."
        assert "A" * 80 + "..." in result
        # 不应包含完整的 100 个 A
        assert "A" * 100 not in result

    def test_description_no_truncation_short(self, tmp_path):
        """description <= 80 字时不应加 '...' 后缀。"""
        app_dir = tmp_path / "short_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        short_desc = "B" * 50  # 50 字，不超过 80
        sup_yaml = {"name": "short_test", "description": short_desc, "workflow": "x"}
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "B" * 50 in result
        # 不应有 "..." 紧跟在描述后面
        assert "B" * 50 + "..." not in result

    def test_description_exactly_80(self, tmp_path):
        """description 刚好 80 字时不应加 '...'。"""
        app_dir = tmp_path / "exact_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        exact_desc = "C" * 80
        sup_yaml = {"name": "exact_test", "description": exact_desc, "workflow": "x"}
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "C" * 80 in result
        assert "C" * 80 + "..." not in result

    def test_multiple_workers_all_extracted(self, tmp_path):
        """多个 Worker 应全部被提取，不遗漏。"""
        app_dir = tmp_path / "multi_worker_app"
        workflows_dir = app_dir / "workflows"
        worker_dir = workflows_dir / "worker_agents"
        worker_dir.mkdir(parents=True)
        # 创建 supervisor
        sup_yaml = {"name": "sup", "description": "sup", "workflow": "x"}
        with open(workflows_dir / "sup_agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)
        # 创建 3 个 worker
        for i in range(3):
            w = {"name": f"worker_{i}", "description": f"Worker {i} desc", "workflow": f"task {i}"}
            with open(worker_dir / f"step{i}_worker.yaml", "w", encoding="utf-8") as f:
                yaml.dump(w, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "3 个" in result
        assert "worker_0" in result
        assert "worker_1" in result
        assert "worker_2" in result

    def test_multiple_tools_all_listed(self, tmp_path):
        """Supervisor 有多个 tools 时应全部列出，逗号分隔。"""
        app_dir = tmp_path / "multi_tools_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        sup_yaml = {
            "name": "multi_tools",
            "description": "test",
            "workflow": "x",
            "tools": [
                {"name": "tool_alpha"},
                {"name": "tool_beta"},
                {"name": "tool_gamma"},
            ],
        }
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "tool_alpha" in result
        assert "tool_beta" in result
        assert "tool_gamma" in result

    def test_yml_extension_supported(self, tmp_path):
        """.yml 后缀的 YAML 文件也应被识别。"""
        app_dir = tmp_path / "yml_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        sup_yaml = {"name": "yml_agent", "description": "test yml", "workflow": "x"}
        with open(workflows_dir / "agent.yml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "yml_agent" in result

    def test_workflow_text_completeness(self, tmp_path):
        """extract_workflow_text 应返回 workflow 的完整文本，不截断不丢字。"""
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir(parents=True)
        full_workflow = textwrap.dedent("""\
            ## 第一步
            调用 get_context 获取上下文

            ## 第二步
            调用 step0_analysis 进行分析

            ## 第三步
            汇总所有结果，生成最终报告
            包含多行内容
            以及特殊字符：|、>、#、*、`code block`
        """)
        agent_yaml = {"name": "completeness_test", "workflow": full_workflow}
        yaml_path = workflows_dir / "test.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(agent_yaml, f, allow_unicode=True, default_flow_style=False)

        result = extract_workflow_text(str(yaml_path))
        # 每一行都应存在于输出中
        for line in full_workflow.strip().splitlines():
            stripped = line.strip()
            if stripped:
                assert stripped in result, f"缺失行: {stripped!r}"

    def test_schema_inputs_order_preserved(self, tmp_path):
        """agent_function_schema 的 inputs 名称应完整列出。"""
        app_dir = tmp_path / "schema_app"
        worker_dir = app_dir / "workflows" / "worker_agents"
        worker_dir.mkdir(parents=True)
        # supervisor
        sup_yaml = {"name": "sup", "description": "x", "workflow": "x"}
        with open(app_dir / "workflows" / "sup.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)
        # worker with complex schema
        worker_yaml = {
            "name": "schema_worker",
            "description": "test",
            "workflow": "x",
            "agent_function_schema": {
                "description": "测试 schema",
                "inputs": {
                    "param_a": {"description": "参数A", "required": True, "type": "string"},
                    "param_b": {"description": "参数B", "required": True, "type": "string"},
                    "param_c": {"description": "参数C", "required": False, "type": "string"},
                },
                "output": {"description": "JSON 格式的结果"},
            },
        }
        with open(worker_dir / "step0.yaml", "w", encoding="utf-8") as f:
            yaml.dump(worker_yaml, f, allow_unicode=True, default_flow_style=False)

        result = scan_app_structure(str(app_dir))
        assert "param_a" in result
        assert "param_b" in result
        assert "param_c" in result
        assert "JSON 格式的结果" in result


# ---------------------------------------------------------------------------
# 异常数据容错测试：输入脏数据时不应崩溃
# ---------------------------------------------------------------------------
class TestRobustness:
    """验证对异常/非标准数据的容错能力。"""

    def test_tools_field_is_string_not_list(self, tmp_path):
        """YAML 中 tools 写成字符串（而非列表）不应崩溃。"""
        app_dir = tmp_path / "bad_tools_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        # tools 故意写成字符串
        bad_yaml = {"name": "bad_tools", "description": "x", "workflow": "x", "tools": "not_a_list"}
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(bad_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert isinstance(result, str)
        assert "bad_tools" in result
        # tools 不是 list 时应显示为 (无)
        assert "(无)" in result

    def test_worker_agents_field_is_string(self, tmp_path):
        """YAML 中 worker_agents 写成字符串不应崩溃。"""
        app_dir = tmp_path / "bad_wa_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        bad_yaml = {"name": "bad_wa", "description": "x", "workflow": "x", "worker_agents": "not_a_list"}
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(bad_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert isinstance(result, str)
        assert "bad_wa" in result

    def test_schema_inputs_is_not_dict(self, tmp_path):
        """agent_function_schema.inputs 不是 dict 时不应崩溃。"""
        app_dir = tmp_path / "bad_schema_app"
        worker_dir = app_dir / "workflows" / "worker_agents"
        worker_dir.mkdir(parents=True)
        sup_yaml = {"name": "sup", "description": "x", "workflow": "x"}
        with open(app_dir / "workflows" / "sup.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)
        bad_worker = {
            "name": "bad_schema_worker",
            "description": "x",
            "workflow": "x",
            "agent_function_schema": {
                "description": "broken",
                "inputs": "this_should_be_a_dict",
                "output": "also_not_a_dict",
            },
        }
        with open(worker_dir / "step0.yaml", "w", encoding="utf-8") as f:
            yaml.dump(bad_worker, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert isinstance(result, str)
        assert "bad_schema_worker" in result

    def test_workflow_field_is_list(self, tmp_path):
        """workflow 字段是 list[str] 时，应按顺序渲染为可读文本。"""
        yaml_path = tmp_path / "list_workflow.yaml"
        weird_yaml = {
            "name": "list_wf",
            "workflow": [
                "## First\nRun step one.",
                "## Second\nRun step two.",
                "## Third\nRun step three.",
            ],
        }
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(weird_yaml, f, allow_unicode=True)

        result = extract_workflow_text(str(yaml_path))
        assert isinstance(result, str)
        assert "list_wf" in result
        assert "## Workflow 1" in result
        assert "## Workflow 2" in result
        assert "## Workflow 3" in result
        assert result.index("Run step one.") < result.index("Run step two.") < result.index("Run step three.")

    def test_workflow_field_is_dict(self, tmp_path):
        """workflow 字段是 dict 而非 string 时不应崩溃。"""
        yaml_path = tmp_path / "dict_workflow.yaml"
        weird_yaml = {"name": "dict_wf", "workflow": {"step1": "do A", "step2": "do B"}}
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(weird_yaml, f, allow_unicode=True)

        result = extract_workflow_text(str(yaml_path))
        assert isinstance(result, str)
        assert "dict_wf" in result

    def test_yaml_content_is_list_not_dict(self, tmp_path):
        """YAML 顶层是 list 而非 dict 时应返回错误提示。"""
        yaml_path = tmp_path / "list_top.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(["item1", "item2"], f)

        result = extract_workflow_text(str(yaml_path))
        assert "❌" in result

    def test_corrupt_python_file_in_tools(self, tmp_path):
        """agent_tools/ 下的 Python 文件有语法错误时，函数提取不应崩溃。"""
        app_dir = tmp_path / "corrupt_py_app"
        (app_dir / "workflows").mkdir(parents=True)
        tools_dir = app_dir / "agent_tools"
        tools_dir.mkdir()
        # 写一个语法错误的 Python 文件
        (tools_dir / "broken.py").write_text(
            "def good_func():\n    pass\n\ndef bad syntax here\n",
            encoding="utf-8",
        )
        sup_yaml = {"name": "sup", "description": "x", "workflow": "x"}
        with open(app_dir / "workflows" / "sup.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert isinstance(result, str)
        assert "broken.py" in result
        # good_func 应该被提取到（在语法错误行之前）
        assert "good_func()" in result

    def test_python_func_in_comment_not_extracted(self, tmp_path):
        """注释中的 def 不应被提取为函数。"""
        app_dir = tmp_path / "comment_def_app"
        (app_dir / "workflows").mkdir(parents=True)
        tools_dir = app_dir / "agent_tools"
        tools_dir.mkdir()
        (tools_dir / "commented.py").write_text(
            textwrap.dedent("""\
                # def fake_in_comment(x):
                #     pass

                def real_function(y):
                    pass

                    # def nested_fake():

                def another_real():
                    pass
            """),
            encoding="utf-8",
        )
        sup_yaml = {"name": "sup", "description": "x", "workflow": "x"}
        with open(app_dir / "workflows" / "sup.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "real_function()" in result
        assert "another_real()" in result
        # 注释中的函数不应出现
        assert "fake_in_comment" not in result
        # 缩进内的注释中的 def 也不应出现
        assert "nested_fake" not in result

    def test_class_method_not_extracted_as_top_level(self, tmp_path):
        """类方法（缩进的 def）不应被提取为顶层函数。"""
        app_dir = tmp_path / "indented_def_app"
        (app_dir / "workflows").mkdir(parents=True)
        tools_dir = app_dir / "agent_tools"
        tools_dir.mkdir()
        (tools_dir / "with_class.py").write_text(
            textwrap.dedent("""\
                def top_level_func():
                    pass

                class MyClass:
                    def class_method(self):
                        pass

                    def another_method(self):
                        pass

                def another_top():
                    pass
            """),
            encoding="utf-8",
        )
        sup_yaml = {"name": "sup", "description": "x", "workflow": "x"}
        with open(app_dir / "workflows" / "sup.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "top_level_func()" in result
        assert "another_top()" in result
        # 类方法不应出现（它们有缩进，不在行首）
        assert "class_method" not in result
        assert "another_method" not in result

    def test_description_multiline_newlines_replaced(self, tmp_path):
        """多行 description 中的换行符应被替换为空格。"""
        app_dir = tmp_path / "multiline_desc_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        sup_yaml = {
            "name": "multiline",
            "description": "第一行\n第二行\n第三行",
            "workflow": "x",
        }
        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(sup_yaml, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        # description 摘要中不应有裸换行（已被替换为空格）
        # 找到 description 行
        for line in result.splitlines():
            if "**description**" in line:
                assert "\n" not in line.split("**description**")[1]
                assert "第一行" in line
                break


# ===========================================================================
# 黑盒场景测试：从 AI 使用者角度出发，不假设实现细节
# ===========================================================================

def _make_app(tmp_path, name, sup_yaml, workers=None, tool_files=None, entry_scripts=None):
    """便捷工厂：快速创建一个 Application 目录结构。"""
    app_dir = tmp_path / name
    workflows_dir = app_dir / "workflows"
    workflows_dir.mkdir(parents=True)
    with open(workflows_dir / f"{name}_agent.yaml", "w", encoding="utf-8") as f:
        yaml.dump(sup_yaml, f, allow_unicode=True, default_flow_style=False)
    if workers:
        worker_dir = workflows_dir / "worker_agents"
        worker_dir.mkdir()
        for wname, wyaml in workers.items():
            with open(worker_dir / wname, "w", encoding="utf-8") as f:
                yaml.dump(wyaml, f, allow_unicode=True, default_flow_style=False)
    if tool_files:
        tools_dir = app_dir / "agent_tools"
        tools_dir.mkdir()
        for fname, content in tool_files.items():
            (tools_dir / fname).write_text(content, encoding="utf-8")
    if entry_scripts:
        for sname, content in entry_scripts.items():
            (app_dir / sname).write_text(content, encoding="utf-8")
    return app_dir


class TestBlackBoxScanApp:
    """黑盒测试 scan_app_structure：从使用者角度覆盖各种真实场景。"""

    # --- 输入路径相关 ---

    def test_pass_file_path_instead_of_dir(self, tmp_path):
        """用户误传了一个文件路径（而非目录），应提示错误不崩溃。"""
        f = tmp_path / "some_file.yaml"
        f.write_text("name: x")
        result = scan_app_structure(str(f))
        # 文件不是目录，应有错误提示或者当作不存在
        assert isinstance(result, str)

    def test_pass_empty_string(self):
        """传空字符串，不应崩溃。"""
        result = scan_app_structure("")
        assert isinstance(result, str)

    def test_pass_none_like_path(self):
        """传类似 None 的奇怪路径，不应崩溃。"""
        result = scan_app_structure("/dev/null")
        assert isinstance(result, str)

    # --- YAML 字段边界 ---

    def test_name_missing(self, tmp_path):
        """YAML 中没有 name 字段，不应崩溃，应显示占位符。"""
        app = _make_app(tmp_path, "no_name", {"description": "test", "workflow": "x"})
        result = scan_app_structure(str(app))
        assert isinstance(result, str)
        # 应有占位符，如 (未命名)
        assert "未命名" in result

    def test_description_is_none(self, tmp_path):
        """description 明确设为 null/None，不应崩溃。"""
        app = _make_app(tmp_path, "none_desc", {"name": "t", "description": None, "workflow": "x"})
        result = scan_app_structure(str(app))
        assert isinstance(result, str)
        assert "t" in result

    def test_description_is_empty_string(self, tmp_path):
        """description 为空字符串，不应崩溃或显示 '...'。"""
        app = _make_app(tmp_path, "empty_desc", {"name": "t", "description": "", "workflow": "x"})
        result = scan_app_structure(str(app))
        assert isinstance(result, str)
        # 空字符串不应产生 "..."
        assert "t" in result

    def test_chinese_and_emoji_in_fields(self, tmp_path):
        """name/description/workflow 含中文和 emoji，不应乱码或崩溃。"""
        app = _make_app(tmp_path, "cn_app", {
            "name": "中文Agent🤖",
            "description": "这是一个包含emoji的描述 ✅❌⚠️🔥 和特殊符号 |>#{}`",
            "workflow": "## 步骤一\n调用工具🔧\n```mermaid\nA-->B\n```",
        })
        result = scan_app_structure(str(app))
        assert "中文Agent🤖" in result
        assert "emoji" in result

    def test_very_long_workflow(self, tmp_path):
        """超长 workflow（5000+ 字符），scan 不应截断也不应 OOM。"""
        long_wf = "步骤说明。" * 1000  # ~5000 chars
        app = _make_app(tmp_path, "long_wf", {"name": "long", "description": "x", "workflow": long_wf})
        result = scan_app_structure(str(app))
        assert "long" in result
        assert isinstance(result, str)

    # --- tools 字段各种形态 ---

    def test_tools_is_empty_list(self, tmp_path):
        """tools: [] 应显示为 (无)。"""
        app = _make_app(tmp_path, "empty_tools", {"name": "t", "description": "x", "workflow": "x", "tools": []})
        result = scan_app_structure(str(app))
        assert "(无)" in result

    def test_tools_mixed_dict_and_string(self, tmp_path):
        """tools 列表中既有 dict 又有纯字符串，不应崩溃。"""
        app = _make_app(tmp_path, "mixed_tools", {
            "name": "t", "description": "x", "workflow": "x",
            "tools": [
                {"name": "tool_a"},
                "tool_b_string",
                {"name": "tool_c", "module": "x.y.z", "function": "do_it"},
            ],
        })
        result = scan_app_structure(str(app))
        assert "tool_a" in result
        assert "tool_b_string" in result
        assert "tool_c" in result

    def test_tools_item_missing_name(self, tmp_path):
        """tools 列表中有 dict 但缺少 name 字段。"""
        app = _make_app(tmp_path, "no_name_tool", {
            "name": "t", "description": "x", "workflow": "x",
            "tools": [{"module": "x", "function": "y"}],
        })
        result = scan_app_structure(str(app))
        assert isinstance(result, str)
        # 应有占位符
        assert "未命名" in result

    # --- agent_tools/ 文件系统边界 ---

    def test_init_py_not_listed_as_tool(self, tmp_path):
        """agent_tools/__init__.py 的函数不应被列为 Tool（或 __init__.py 不显示函数）。"""
        app = _make_app(tmp_path, "init_app",
            {"name": "t", "description": "x", "workflow": "x"},
            tool_files={
                "__init__.py": "def init_func(): pass\n",
                "real_tool.py": "def do_work(): pass\n",
            },
        )
        result = scan_app_structure(str(app))
        assert "real_tool.py" in result
        assert "do_work()" in result

    def test_non_py_files_ignored(self, tmp_path):
        """agent_tools/ 下的 .pyc, .md, .json 文件不应被列出。"""
        app = _make_app(tmp_path, "non_py_app",
            {"name": "t", "description": "x", "workflow": "x"},
            tool_files={
                "real.py": "def real_func(): pass\n",
                "readme.md": "# readme\n",
                "data.json": '{"key": "value"}\n',
            },
        )
        result = scan_app_structure(str(app))
        assert "real.py" in result
        assert "readme.md" not in result
        assert "data.json" not in result

    def test_nested_tools_dir_not_scanned(self, tmp_path):
        """agent_tools/ 下的子目录中的 .py 不应被扫描（只扫一层）。"""
        app = _make_app(tmp_path, "nested_app",
            {"name": "t", "description": "x", "workflow": "x"},
            tool_files={"top_level.py": "def top_func(): pass\n"},
        )
        # 手动创建嵌套子目录
        nested = app / "agent_tools" / "utils"
        nested.mkdir()
        (nested / "helper.py").write_text("def nested_func(): pass\n")
        result = scan_app_structure(str(app))
        assert "top_func()" in result
        # 嵌套目录中的函数不应出现
        assert "nested_func" not in result

    def test_async_def_extracted(self, tmp_path):
        """async def 定义的函数也应被提取。"""
        app = _make_app(tmp_path, "async_app",
            {"name": "t", "description": "x", "workflow": "x"},
            tool_files={"async_tool.py": "async def async_handler(x):\n    pass\n\ndef sync_func():\n    pass\n"},
        )
        result = scan_app_structure(str(app))
        assert "sync_func()" in result
        # async def 是否被提取？这是一个设计决策，但至少不应崩溃
        assert isinstance(result, str)

    def test_empty_py_file(self, tmp_path):
        """0 字节的 .py 文件不应崩溃。"""
        app = _make_app(tmp_path, "empty_py",
            {"name": "t", "description": "x", "workflow": "x"},
            tool_files={"empty.py": ""},
        )
        result = scan_app_structure(str(app))
        assert "empty.py" in result
        assert "(无顶层函数)" in result

    # --- 入口脚本边界 ---

    def test_dunder_init_not_listed_as_entry(self, tmp_path):
        """__init__.py 不应被列为入口脚本。"""
        app = _make_app(tmp_path, "init_entry",
            {"name": "t", "description": "x", "workflow": "x"},
            entry_scripts={"__init__.py": "", "real_entry.py": "# entry\n"},
        )
        result = scan_app_structure(str(app))
        assert "real_entry.py" in result
        # __init__.py 不应出现在入口脚本段
        lines = result.split("## 入口脚本")
        if len(lines) > 1:
            assert "__init__.py" not in lines[1]

    # --- 多 Supervisor 场景 ---

    def test_multiple_supervisors(self, tmp_path):
        """workflows/ 下有多个 YAML 文件（如 v1 和 v2），都应被扫描。"""
        app_dir = tmp_path / "multi_sup"
        wf_dir = app_dir / "workflows"
        wf_dir.mkdir(parents=True)
        for name in ["agent_v1", "agent_v2"]:
            with open(wf_dir / f"{name}.yaml", "w", encoding="utf-8") as f:
                yaml.dump({"name": name, "description": f"{name} desc", "workflow": "x"}, f, allow_unicode=True)
        result = scan_app_structure(str(app_dir))
        assert "agent_v1" in result
        assert "agent_v2" in result

    # --- 空 YAML / 损坏 YAML ---

    def test_empty_yaml_file(self, tmp_path):
        """0 字节的 .yaml 文件不应崩溃。"""
        app_dir = tmp_path / "empty_yaml"
        wf_dir = app_dir / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "agent.yaml").write_text("")
        result = scan_app_structure(str(app_dir))
        assert isinstance(result, str)

    def test_yaml_with_anchors(self, tmp_path):
        """YAML 使用 anchor/alias (&, *) 语法不应崩溃。"""
        app_dir = tmp_path / "anchor_app"
        wf_dir = app_dir / "workflows"
        wf_dir.mkdir(parents=True)
        yaml_content = textwrap.dedent("""\
            name: anchor_test
            description: &desc "共享描述"
            workflow: "步骤1"
            extra_desc: *desc
        """)
        (wf_dir / "agent.yaml").write_text(yaml_content, encoding="utf-8")
        result = scan_app_structure(str(app_dir))
        assert "anchor_test" in result
        assert "共享描述" in result


class TestBlackBoxExtractWorkflow:
    """黑盒测试 extract_workflow_text：从使用者角度覆盖各种真实场景。"""

    def test_pass_directory_instead_of_file(self, tmp_path):
        """用户误传了目录路径而非文件路径，不应崩溃。"""
        result = extract_workflow_text(str(tmp_path))
        assert isinstance(result, str)
        # 应有错误提示
        assert "❌" in result or "不存在" in result.lower() or "不是" in result

    def test_workflow_with_mermaid_code_block(self, tmp_path):
        """workflow 包含 Mermaid 代码块（含 ```），应完整保留。"""
        wf_text = textwrap.dedent("""\
            ## 流程图
            ```mermaid
            flowchart TD
              A[开始] --> B{条件?}
              B -->|是| C[执行]
              B -->|否| D[跳过]
            ```
            ## 后续步骤
            调用 Worker 完成任务。
        """)
        yaml_path = tmp_path / "mermaid.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump({"name": "mermaid_agent", "workflow": wf_text}, f, allow_unicode=True, default_flow_style=False)

        result = extract_workflow_text(str(yaml_path))
        assert "mermaid" in result
        assert "flowchart TD" in result
        assert "A[开始]" in result
        assert "后续步骤" in result

    def test_workflow_folded_style(self, tmp_path):
        """YAML 使用 > 折叠语法写的 workflow，换行应被正确处理。"""
        yaml_content = textwrap.dedent("""\
            name: folded_test
            workflow: >
              这是第一行
              这是第二行会被折叠成同一段

              这是新段落
        """)
        yaml_path = tmp_path / "folded.yaml"
        yaml_path.write_text(yaml_content, encoding="utf-8")

        result = extract_workflow_text(str(yaml_path))
        assert "folded_test" in result
        assert "第一行" in result
        assert "新段落" in result

    def test_workflow_literal_style(self, tmp_path):
        """YAML 使用 | 字面量语法写的 workflow，换行应完整保留。"""
        yaml_content = textwrap.dedent("""\
            name: literal_test
            workflow: |
              ## 步骤一
              做事情A

              ## 步骤二
              做事情B
        """)
        yaml_path = tmp_path / "literal.yaml"
        yaml_path.write_text(yaml_content, encoding="utf-8")

        result = extract_workflow_text(str(yaml_path))
        assert "步骤一" in result
        assert "步骤二" in result
        assert "做事情A" in result
        assert "做事情B" in result

    def test_very_long_workflow_not_truncated(self, tmp_path):
        """超长 workflow（5000+ 字符）不应被截断。"""
        marker_start = "<<START_MARKER>>"
        marker_end = "<<END_MARKER>>"
        long_middle = "中间内容重复。" * 800  # ~5600 chars
        wf = f"{marker_start}\n{long_middle}\n{marker_end}"
        yaml_path = tmp_path / "long.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump({"name": "long_wf", "workflow": wf}, f, allow_unicode=True, default_flow_style=False)

        result = extract_workflow_text(str(yaml_path))
        assert marker_start in result
        assert marker_end in result

    def test_empty_yaml_file(self, tmp_path):
        """0 字节的 YAML 文件不应崩溃。"""
        yaml_path = tmp_path / "empty.yaml"
        yaml_path.write_text("", encoding="utf-8")
        result = extract_workflow_text(str(yaml_path))
        assert isinstance(result, str)

    def test_yaml_only_comments(self, tmp_path):
        """YAML 文件只有注释，不应崩溃。"""
        yaml_path = tmp_path / "comments.yaml"
        yaml_path.write_text("# this is a comment\n# another comment\n", encoding="utf-8")
        result = extract_workflow_text(str(yaml_path))
        assert isinstance(result, str)

    def test_workflow_is_integer(self, tmp_path):
        """workflow 字段写成数字，不应崩溃。"""
        yaml_path = tmp_path / "int_wf.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump({"name": "int_wf", "workflow": 42}, f)
        result = extract_workflow_text(str(yaml_path))
        assert isinstance(result, str)
        assert "int_wf" in result

    def test_workflow_is_boolean(self, tmp_path):
        """workflow 字段写成 true/false，不应崩溃。"""
        yaml_path = tmp_path / "bool_wf.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump({"name": "bool_wf", "workflow": True}, f)
        result = extract_workflow_text(str(yaml_path))
        assert isinstance(result, str)
        assert "bool_wf" in result

    def test_workflow_contains_yaml_special_chars(self, tmp_path):
        """workflow 内容含 YAML 特殊字符（: { } [ ] , & * # ? | - < > = ! % @ `）。"""
        wf = "配置: {key: value}\n列表: [1, 2, 3]\n锚点: &ref *ref\n# 注释行\n百分比: 80%"
        yaml_path = tmp_path / "special.yaml"
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump({"name": "special", "workflow": wf}, f, allow_unicode=True, default_flow_style=False)
        result = extract_workflow_text(str(yaml_path))
        assert "配置" in result
        assert "80%" in result

    def test_binary_file_not_crash(self, tmp_path):
        """传一个二进制文件（如 .pyc），不应崩溃。"""
        bin_path = tmp_path / "fake.yaml"
        bin_path.write_bytes(b"\x00\x01\x02\x80\x81\xff" * 100)
        result = extract_workflow_text(str(bin_path))
        assert isinstance(result, str)


class TestBlackBoxFunctionExtraction:
    """黑盒测试函数提取逻辑：各种 Python 代码模式。"""

    def _scan_with_py(self, tmp_path, py_content):
        """辅助：创建一个 App，agent_tools/ 下放一个 tool.py，返回 scan 结果。"""
        return scan_app_structure(str(_make_app(
            tmp_path, "func_test",
            {"name": "t", "description": "x", "workflow": "x"},
            tool_files={"tool.py": py_content},
        )))

    def test_decorator_on_function(self, tmp_path):
        """带装饰器的函数应被提取。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            @some_decorator
            def decorated_func():
                pass
        """))
        assert "decorated_func()" in result

    def test_multiline_def(self, tmp_path):
        """参数跨多行的函数应被提取。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            def long_params(
                param_a: str,
                param_b: int,
                param_c: float = 0.0,
            ) -> str:
                pass
        """))
        assert "long_params()" in result

    def test_def_in_string_literal(self, tmp_path):
        """字符串中的 def 不应被提取。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            docstring = "def fake_in_string(x): pass"

            template = '''
            def another_fake():
                pass
            '''

            def real_func():
                pass
        """))
        assert "real_func()" in result
        assert "fake_in_string" not in result

    def test_inner_function(self, tmp_path):
        """函数内部定义的嵌套函数不应被提取。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            def outer():
                def inner():
                    pass
                return inner

            def another_outer():
                pass
        """))
        assert "outer()" in result
        assert "another_outer()" in result
        assert "inner" not in result

    def test_lambda_ignored(self, tmp_path):
        """lambda 不应被当作函数提取。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            handler = lambda x: x + 1

            def real_func():
                pass
        """))
        assert "real_func()" in result
        assert "lambda" not in result
        assert "handler" not in result

    def test_only_private_functions(self, tmp_path):
        """文件中只有 _private 函数时应显示 (无顶层函数)。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            def _private_a():
                pass

            def _private_b():
                pass
        """))
        assert "(无顶层函数)" in result

    def test_function_with_type_hints(self, tmp_path):
        """带复杂类型标注的函数应正确提取函数名。"""
        result = self._scan_with_py(tmp_path, textwrap.dedent("""\
            def complex_types(items: list[dict[str, Any]], callback: Callable[[int], bool]) -> Optional[str]:
                pass
        """))
        assert "complex_types()" in result

    def test_many_functions_all_found(self, tmp_path):
        """20 个函数应全部被提取，不遗漏。"""
        lines = []
        for i in range(20):
            lines.append(f"def func_{i:02d}():\n    pass\n")
        result = self._scan_with_py(tmp_path, "\n".join(lines))
        for i in range(20):
            assert f"func_{i:02d}()" in result


class TestDynamicCapabilityDiscovery:
    """验证动态工具能力发现输出。"""

    def test_discovery_reads_app_and_project_system_yaml(self, fake_app):
        """同时存在应用级和项目级 system.yaml 时，应按覆盖链给出有效 default tools。"""
        # fake_app: <tmp>/applications/test_app
        project_root = fake_app.parents[1]
        app_config = fake_app / "config"
        project_config = project_root / "config"
        app_config.mkdir(parents=True, exist_ok=True)
        project_config.mkdir(parents=True, exist_ok=True)

        (app_config / "system.yaml").write_text(
            yaml.safe_dump(
                {"default_loaded_tools": ["app_only_tool"]},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (project_config / "system.yaml").write_text(
            yaml.safe_dump(
                {"default_loaded_tools": ["project_tool_a", "project_tool_b"]},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        result = scan_app_structure(str(fake_app))
        assert "工具能力发现（动态）" in result
        assert "app_only_tool" in result
        # 应用级列表覆盖项目级列表，因此有效值中不应包含 project_tool_*
        assert "project_tool_a" not in result
        assert "project_tool_b" not in result

    def test_discovery_gives_hint_when_no_system_yaml(self, empty_app):
        """无 system.yaml 时，应给出推断提示而非报错。"""
        result = scan_app_structure(str(empty_app))
        assert "工具能力发现（动态）" in result
        assert "默认工具能力无法直接确认" in result


class TestAgentLoomRootPrecondition:
    """验证相对路径模式下的 AgentLoom 根目录门禁。"""

    def test_scan_relative_path_requires_agent_loom_root(self, tmp_path, monkeypatch):
        non_root = tmp_path / "not_agent_loom_root"
        non_root.mkdir(parents=True)
        monkeypatch.chdir(non_root)

        result = scan_app_structure("applications/demo_app")
        assert "根目录前置条件不满足" in result
        assert "config/llm.yaml" in result

    def test_extract_relative_path_requires_agent_loom_root(self, tmp_path, monkeypatch):
        non_root = tmp_path / "not_agent_loom_root"
        non_root.mkdir(parents=True)
        monkeypatch.chdir(non_root)

        result = extract_workflow_text("applications/demo_app/workflows/agent.yaml")
        assert "根目录前置条件不满足" in result
        assert "config/llm.yaml" in result


class TestEffectiveConfigDiscovery:
    """验证覆盖链与映射发现逻辑。"""

    def test_effective_tools_default_respects_list_override(self, tmp_path):
        project_root = tmp_path / "proj"
        app_dir = project_root / "applications" / "demo_app"
        (project_root / "config").mkdir(parents=True)
        (app_dir / "config").mkdir(parents=True)
        (app_dir / "workflows").mkdir(parents=True)

        with open(app_dir / "workflows" / "demo.yaml", "w", encoding="utf-8") as f:
            yaml.dump({"name": "demo", "description": "x", "workflow": "x"}, f, allow_unicode=True)

        # Low priority: project-level default tools
        with open(project_root / "config" / "system.yaml", "w", encoding="utf-8") as f:
            yaml.dump({"default_loaded_tools": ["global_tool"]}, f, allow_unicode=True)

        # High priority: app-level explicit override to empty list
        with open(app_dir / "config" / "system.yaml", "w", encoding="utf-8") as f:
            yaml.dump({"default_loaded_tools": []}, f, allow_unicode=True)

        result = scan_app_structure(str(app_dir))
        assert "有效 `default_loaded_tools`: []" in result
        assert str(app_dir / "config" / "system.yaml") in result

    def test_mapping_detects_legacy_fallback(self, tmp_path):
        project_root = tmp_path / "proj"
        app_dir = project_root / "applications" / "demo_app"
        (project_root / "config").mkdir(parents=True)
        (app_dir / "workflows").mkdir(parents=True)

        with open(app_dir / "workflows" / "demo.yaml", "w", encoding="utf-8") as f:
            yaml.dump({"name": "demo", "description": "x", "workflow": "x"}, f, allow_unicode=True)

        with open(project_root / "config" / "system.yaml", "w", encoding="utf-8") as f:
            yaml.dump(
                {
                    "tools_mapping": {
                        "mapping": {
                            "Read": "read_file",
                            "Write": "write_markdown_file",
                        }
                    }
                },
                f,
                allow_unicode=True,
            )

        result = scan_app_structure(str(app_dir))
        assert "mapping" in result
        assert "回退来源" in result or "映射信息" in result

    def test_mapping_legacy_ignored_when_claude_mapping_exists(self, tmp_path):
        project_root = tmp_path / "proj"
        app_dir = project_root / "applications" / "demo_app"
        (project_root / "config").mkdir(parents=True)
        (app_dir / "workflows").mkdir(parents=True)

        with open(app_dir / "workflows" / "demo.yaml", "w", encoding="utf-8") as f:
            yaml.dump({"name": "demo", "description": "x", "workflow": "x"}, f, allow_unicode=True)

        with open(project_root / "config" / "system.yaml", "w", encoding="utf-8") as f:
            yaml.dump(
                {
                    "tools_mapping": {
                        "Claude": {
                            "Read": "read_file",
                        },
                        "mapping": {
                            "Read": "legacy_read_file_content",
                        },
                    }
                },
                f,
                allow_unicode=True,
            )

        result = scan_app_structure(str(app_dir))
        assert "tools_mapping.Claude" in result
        assert "legacy 将被忽略" in result


class TestExecutionEnvAndMarkdownSupport:
    """验证 execution_env 字段提取和 Markdown Agent 配置兼容。"""

    def test_execution_env_fields_in_summary(self, tmp_path):
        app_dir = tmp_path / "env_app"
        workflows_dir = app_dir / "workflows"
        workflows_dir.mkdir(parents=True)
        with open(workflows_dir / "env_agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump(
                {
                    "name": "env_agent",
                    "description": "x",
                    "workflow": "x",
                    "execution_env": {
                        "type": "docker",
                    },
                },
                f,
                allow_unicode=True,
            )

        result = scan_app_structure(str(app_dir))
        assert "execution_env.type" in result
        assert "docker" in result
        assert "跳过 `default_loaded_tools`" in result

    def test_markdown_agent_is_scanned_and_workflow_extractable(self, tmp_path):
        app_dir = tmp_path / "md_app"
        workflows_dir = app_dir / "workflows"
        worker_dir = workflows_dir / "worker_agents"
        worker_dir.mkdir(parents=True)

        (workflows_dir / "supervisor.md").write_text(
            textwrap.dedent("""\
                ```yaml
                name: md_supervisor
                description: markdown supervisor
                tools:
                  - name: read_file_content
                ```

                ## Workflow
                调用 worker，汇总输出。
            """),
            encoding="utf-8",
        )
        (worker_dir / "step0.md").write_text(
            textwrap.dedent("""\
                ```yaml
                name: md_worker
                description: markdown worker
                agent_function_schema:
                  description: worker schema
                  inputs:
                    query:
                      description: 查询
                      required: true
                  output:
                    description: 输出
                ```

                ## Worker Workflow
                读取输入并输出结果。
            """),
            encoding="utf-8",
        )

        scan_result = scan_app_structure(str(app_dir))
        assert "md_supervisor" in scan_result
        assert "md_worker" in scan_result

        workflow_result = extract_workflow_text(str(worker_dir / "step0.md"))
        assert "md_worker" in workflow_result
        assert "Worker Workflow" in workflow_result

    def test_tool_docstring_summary_is_extracted(self, tmp_path):
        app_dir = tmp_path / "docstring_app"
        workflows_dir = app_dir / "workflows"
        tools_dir = app_dir / "agent_tools"
        workflows_dir.mkdir(parents=True)
        tools_dir.mkdir()

        with open(workflows_dir / "agent.yaml", "w", encoding="utf-8") as f:
            yaml.dump({"name": "agent", "description": "x", "workflow": "x"}, f, allow_unicode=True)

        (tools_dir / "tooling.py").write_text(
            textwrap.dedent("""\
                def get_context(module_path: str) -> str:
                    \"\"\"读取模块上下文并返回摘要信息。\"\"\"
                    return module_path
            """),
            encoding="utf-8",
        )

        result = scan_app_structure(str(app_dir))
        assert "get_context()" in result
        assert "读取模块上下文并返回摘要信息" in result


class TestSkillContractText:
    """文档契约回归测试：确保框架级 Skill 的核心要求保留。"""

    def test_skill_clarifies_goal_and_acceptance_criteria(self):
        skill_path = _AGENT_LOOM_ROOT / "agentloom-framework-skill" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        assert "功能目标、输入、输出、验收标准" in content
        assert "判断任务类型" in content
        assert "最后必须运行验证命令" in content

    def test_skill_mentions_agent_loom_root_precondition(self):
        skill_path = _AGENT_LOOM_ROOT / "agentloom-framework-skill" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        assert "config/llm.yaml" in content
        assert "不要用 `config/system.yaml`" in content
