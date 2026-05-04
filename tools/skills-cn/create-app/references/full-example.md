# 完整示例：从零创建 `code_review` Application

> 本文档展示了从需求收集到最终文件生成的完整流程，作为 Skill 执行的参考样本。
>
> **所有路径均相对于项目根目录**（即包含 `config/llm.yaml` 的目录）。

---

## 示例场景

用户需求："帮我创建一个 Code Review Application，可以对指定的 Git 仓库进行多维度代码审查"

---

## 第一阶段：需求收集结果

| 问题 | 用户回答 |
|------|----------|
| Q1 Application 名称 | `code_review` |
| Q2 一句话描述 | `对 Git 仓库进行多维度代码审查并输出结构化审查报告` |
| Q3 Worker 阶段 | 3 个阶段：① 代码扫描与结构分析 ② 风险评估与问题标记 ③ 报告汇总 |
| Q4 model_type 可用选项发现 | 读取 `config/llm.yaml`，发现可用类型：`common` / `powerful` / `fast` / `summary`，`default_model_type=powerful` |
| Q5 Supervisor model_type 策略 | 使用 `default_model_type`（不明确指定） |
| Q6 Worker model_type 策略 | 与 Supervisor 相同（不明确指定） |
| Q7 tool_call_type | `code_act` |
| Q8 自定义 Tool | 一个 `get_repo_context` Tool，用于获取仓库概览 |
| Q9 Worker Tool | `read_file`、`get_file_outline`、`ripgrep_search_directory` |
| Q10 planning_interval | Worker 设为 `3` |
| Q11 max_steps | 默认 `80` |
| Q12 App 级配置 | 不需要 |
| Q13 自定义 sysprompt | 不需要 |
| Q14 execution_env | `local` |
| Q15 私有 Skill | 不需要 |

---

## 第二阶段：计划确认

```
📋 Application 生成计划

名称：code_review
描述：对 Git 仓库进行多维度代码审查并输出结构化审查报告
模式：Supervisor + 3 Worker

目录结构预览：
applications/code_review/
├── code_review_app.py
├── agent_tools/
│   └── repo_context.py
└── workflows/
    ├── code_review_agent.yaml
    └── worker_agents/
        ├── code_scan.yaml
        ├── risk_assessment.yaml
        └── report_generation.yaml

Supervisor 配置摘要：
- name: code_review_agent
- model_type: 继承 default_model_type（不明确指定）
- tool_call_type: code_act
- 自定义 Tool：get_repo_context
- Worker 数量：3

Worker 配置摘要：
| 阶段 | 名称                    | 职责              | model_type 策略 | Tool                                                   |
|------|-------------------------|--------------------|-----------------|--------------------------------------------------------|
| 1    | code_scan               | 代码扫描与结构分析 | 继承默认        | read_file, get_file_outline, ripgrep_search_directory |
| 2    | risk_assessment         | 风险评估与问题标记 | 继承默认        | read_file, ripgrep_search_directory             |
| 3    | report_generation       | 报告汇总           | 继承默认        | write_markdown_file                                     |
```

用户确认：✅

---

## 第三阶段：生成的文件

### 文件 1：`applications/code_review/workflows/code_review_agent.yaml`

```yaml
name: "code_review_agent"
description: |
  作为 Code Review Supervisor Agent，你的核心职责是协调多个专业 Worker Agent
  对目标 Git 仓库进行多维度代码审查，并生成结构化审查报告。

tool_call_type: "code_act"

workflow: |
  # Code Review 工作流

  ## 背景
  你是一名**资深代码审查架构师**。当前任务是对目标代码仓库进行系统化的
  多维度审查。

  ## 执行流程

  ```mermaid
  flowchart TD
    A[调用 get_repo_context 获取仓库概览] --> B[调用 code_scan 进行代码扫描]
    B --> C[调用 risk_assessment 进行风险评估]
    C --> D[调用 report_generation 生成报告]
    D --> E[输出最终审查报告]
  ```

  ## 执行原则
  1. **先获取上下文**：必须先调用 `get_repo_context` 了解仓库整体结构
  2. **按顺序调用 Worker**：严格按照 step0 → step1 → step2 的顺序执行
  3. **传递上下文**：每个 Worker 的输出作为下一个 Worker 的输入
  4. **汇总报告**：将 step2 的输出作为完整审查报告呈现

  ## 输出要求
  - 输出 report_generation 返回的完整 Markdown 报告
  - 如果某个 Worker 失败，报告失败的阶段和原因

tools:
  - name: "read_file"
  - name: "browse_directory"
  - name: "get_repo_context"
    module: "applications.code_review.agent_tools.repo_context"
    function: "get_repo_context"

# Worker YAML 必须放在 workflows/worker_agents/ 目录下
# 简写形式（同 app 下的 Worker 推荐使用）：
worker_agents:
  - path: "code_scan.yaml"
  - path: "risk_assessment.yaml"
  - path: "report_generation.yaml"
# 等价的完整相对路径形式（跨 app 引用时使用）：
#  - path: "applications/code_review/workflows/worker_agents/code_scan.yaml"
#  - path: "applications/code_review/workflows/worker_agents/risk_assessment.yaml"
#  - path: "applications/code_review/workflows/worker_agents/report_generation.yaml"

execution_env:
  type: "local"
```

---

### 文件 2：`applications/code_review/workflows/worker_agents/code_scan.yaml`

```yaml
name: "code_scan"
description: "代码结构扫描与分析 Worker"
tool_call_type: "code_act"
planning_interval: 3
max_steps: 40

tools:
  - name: "read_file"
  - name: "get_file_outline"
  - name: "ripgrep_search_directory"
  - name: "browse_directory"

workflow: |
  # 步骤 0：代码扫描与结构分析

  ## 背景
  你是一名**资深代码结构分析工程师**，负责对目标代码仓库进行
  全面扫描。

  ## 核心职责
  1. **目录结构扫描**：列出所有源代码目录和关键文件
  2. **模块依赖分析**：识别模块间的导入依赖关系
  3. **代码规模统计**：文件数量、代码行数、函数/类数量
  4. **架构模式识别**：识别使用的设计模式和架构风格

  ## 约束
  - ❌ 不要进行任何风险评估或质量判断（那是 risk_assessment 的职责）
  - ❌ 不要修改任何源代码文件
  - ✅ 所有分析必须基于实际代码内容——不要推测

  ## 输出要求
  - 格式：结构化 Markdown 文本
  - 必须包含：文件树、模块依赖图、代码规模统计表
  - 不确定的推断标记为【推断】

agent_function_schema:
  description: |
    代码结构扫描与分析工具。扫描目标代码仓库的目录结构、模块依赖和
    代码规模，输出结构化分析报告。
  inputs:
    query:
      description: "扫描任务描述，包括目标仓库路径和关注重点"
      required: true
  output:
    description: "Markdown 格式的代码结构分析报告"
```

---

### 文件 3：`applications/code_review/workflows/worker_agents/risk_assessment.yaml`

```yaml
name: "risk_assessment"
description: "风险评估与问题标记 Worker"
tool_call_type: "code_act"
planning_interval: 3
max_steps: 40

tools:
  - name: "read_file"
  - name: "ripgrep_search_directory"
  - name: "read_file"

workflow: |
  # 步骤 1：风险评估与问题标记

  ## 背景
  你是一名**资深代码安全与质量审计工程师**，负责根据 code_scan 的结构分析结果，
  对代码中的风险点和质量问题进行深入检查。

  ## 核心职责
  1. **安全风险扫描**：SQL 注入、XSS、硬编码凭证、不安全的依赖
  2. **代码质量问题**：重复代码、过长函数、过高复杂度、缺少错误处理
  3. **最佳实践违规**：命名规范、缺少注释、不规范的日志记录
  4. **严重级别标记**：为每个问题标记 Critical / High / Medium / Low

  ## 约束
  - ❌ 不要修改任何源代码文件
  - ❌ 不要提供修复建议（那是 report_generation 的职责）
  - ✅ 每个问题必须包含精确的 `文件:行号` 位置
  - ✅ 不确定的问题标记置信度（高/中/低）

  ## 输出要求
  - 格式：问题清单表格
  - 必须包含：问题 ID、严重级别、问题描述、文件:行号、置信度
  - 按严重级别降序排列

agent_function_schema:
  description: |
    代码风险评估与问题标记工具。基于代码结构分析结果，对安全风险和
    质量问题进行深入检查，输出带严重级别标记的问题清单。
  inputs:
    query:
      description: "审查任务描述，包括 step0 分析结果摘要和审查关注重点"
      required: true
  output:
    description: "Markdown 格式的风险评估报告（问题清单表格）"
```

---

### 文件 4：`applications/code_review/workflows/worker_agents/report_generation.yaml`

```yaml
name: "report_generation"
description: "审查报告汇总生成 Worker"
tool_call_type: "code_act"
planning_interval: 3
max_steps: 40

tools:
  - name: "write_markdown_file"

workflow: |
  # 步骤 2：报告汇总

  ## 背景
  你是一名**技术写作专家**，负责将前序 Worker 的分析结果整合为一份
  结构清晰、可读性强的代码审查报告。

  ## 核心职责
  1. **整合汇总**：合并 code_scan（结构分析）和 risk_assessment（风险评估）的输出
  2. **优先级排序**：按业务影响和修复成本对问题排序
  3. **修复建议**：为每个 Critical/High 问题提供具体修复建议
  4. **执行摘要**：在报告开头编写面向管理层的执行摘要

  ## 约束
  - ❌ 不要遗漏任何 Critical/High 严重级别的问题
  - ❌ 不要编造 step0/step1 中未提及的问题
  - ✅ 修复建议必须具体到代码层面（函数名、文件路径）

  ## 输出格式

  ```markdown
  # 代码审查报告

  ## 执行摘要
  <2-3 句话总结审查结论>

  ## 1. 代码结构概览
  <来自 step0 的结构分析>

  ## 2. 问题清单
  ### 2.1 Critical
  ### 2.2 High
  ### 2.3 Medium
  ### 2.4 Low

  ## 3. 修复建议
  <针对 Critical/High 问题的具体修复方案>

  ## 4. 总结与后续步骤
  ```

agent_function_schema:
  description: |
    审查报告汇总生成工具。将代码结构分析和风险评估结果整合为一份
    完整的 Markdown 格式代码审查报告。
  inputs:
    query:
      description: "报告生成任务，包括 step0 和 step1 的完整输出"
      required: true
  output:
    description: "完整的 Markdown 格式代码审查报告"
```

---

### 文件 5：`applications/code_review/agent_tools/repo_context.py`

```python
"""
仓库上下文生成工具。

动态加载的 Tool 是纯 Python 函数——无需装饰器。
框架通过 YAML 中的 module + function 配置动态导入。
Tool 描述自动从函数的 docstring 中提取。
"""

import os
import subprocess


def get_repo_context(repo_path: str) -> str:
    """
    获取目标 Git 仓库的上下文概览信息。

    扫描仓库目录结构、Git 信息、README 内容等，
    为后续代码审查提供基础上下文。

    Args:
        repo_path: 目标仓库路径（相对或绝对路径）

    Returns:
        Markdown 格式的仓库概览文本，包括目录结构、
        Git 分支信息、README 摘要等。
    """
    abs_path = os.path.abspath(repo_path)
    if not os.path.isdir(abs_path):
        return f"Error: Path {abs_path} does not exist or is not a directory"

    sections = []
    sections.append(f"# Repository Overview: {os.path.basename(abs_path)}\n")

    # 目录结构（最多 3 层）
    try:
        result = subprocess.run(
            ["find", abs_path, "-maxdepth", "3", "-type", "f", "-name", "*.py"],
            capture_output=True, text=True, timeout=10
        )
        py_files = result.stdout.strip().split("\n")
        sections.append(f"## Python File Count: {len(py_files)}\n")
    except Exception as e:
        sections.append(f"## File Scan Failed: {e}\n")

    # Git 信息
    try:
        branch = subprocess.run(
            ["git", "-C", abs_path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5
        )
        sections.append(f"## Current Branch: {branch.stdout.strip()}\n")
    except Exception:
        sections.append("## Git Info: Unavailable\n")

    # README
    readme_path = os.path.join(abs_path, "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read(2000)  # 仅读取前 2000 个字符
        sections.append(f"## README Summary\n{content}\n")

    return "\n".join(sections)
```

---

### 文件 6：`applications/code_review/code_review_app.py`

```python
#!/usr/bin/env python3
"""
code_review – Application 入口脚本。

运行方式：
    PROJECT_ROOT=/path/to/project
    cd "$PROJECT_ROOT"
    .venv/bin/python applications/code_review/code_review_app.py

或使用 runner：
    PROJECT_ROOT=/path/to/project
    cd "$PROJECT_ROOT"
    .venv/bin/python src/runner.py applications/code_review/workflows/code_review_agent.yaml

或使用 AgentLoom CLI（如已安装）：
    cd "$PROJECT_ROOT"
    loom run applications/code_review/workflows/code_review_agent.yaml
"""

import os
import sys

# 确保项目根目录在 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.runner import run_app


def main():
    run_app("applications/code_review/workflows/code_review_agent.yaml")


if __name__ == "__main__":
    main()
```

---

## 第四阶段：运行指南

```bash
# 方法一：使用 runner（推荐）
cd <project_root>
<python> src/runner.py applications/code_review/workflows/code_review_agent.yaml

# 方法二：使用入口脚本
cd <project_root>
<python> applications/code_review/code_review_app.py
```

---

## 单 Agent 模式示例：`simple_scanner`

如果你只需要一个独立运行的 Agent，不需要 Supervisor/Worker 编排：

### 目录结构

```
applications/simple_scanner/
├── simple_scanner_app.py
└── workflows/
    └── simple_scanner_agent.yaml    # 独立 Agent，无 Supervisor 编排
```

### `simple_scanner_agent.yaml`

```yaml
name: "simple_scanner"
description: "一个简单的代码扫描 Agent，独立运行，无需 Supervisor 编排"
model_type: "fast"
tool_call_type: "code_act"
max_steps: 30

tools:
  - name: "read_file"
  - name: "browse_directory"
  - name: "list_files_glob"
  - name: "ripgrep_search_directory"

workflow: |
  # 简单代码扫描

  ## 背景
  你是一名**代码扫描助手**，负责快速扫描指定目录中的代码文件。

  ## 任务
  1. 浏览目标目录结构
  2. 统计每种语言的文件数量和代码行数
  3. 输出简洁的扫描报告

  ## 输出要求
  - Markdown 格式
  - 包含文件统计表和目录结构树

# 单 Agent 模式：不需要 worker_agents 或 agent_function_schema，直接使用 runner 运行
```

### 运行方式

```bash
cd <project_root>
<python> src/runner.py applications/simple_scanner/workflows/simple_scanner_agent.yaml
```
