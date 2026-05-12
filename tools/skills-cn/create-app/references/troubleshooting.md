# 常见问题排查指南

> AgentLoom Application 配置与运行时的常见错误及解决方案。

---

## 1. Worker 加载失败

### 症状
```
Error: Failed to load worker agent: ...
```

### 排查步骤

| 检查项 | 说明 |
|--------|------|
| `worker_agents` 用了 `name` 而非 `path` | ❌ `name: code_scan` → ✅ `path: "applications/.../code_scan.yaml"` |
| `path` 拼写错误 | 确认文件确实存在，注意大小写 |
| `path` 解析规则理解有误 | 仅文件名（不含 `/`）从 `worker_agents/` 目录解析；含 `/` 的相对路径从项目根目录解析 |
| Worker YAML 语法错误 | 执行 `python -c "import yaml; yaml.safe_load(open('path/to/worker.yaml'))"` 检查 |
| Worker 缺少 `agent_function_schema` | Worker 必须定义此字段才能被 Supervisor 作为工具调用 |

> 补充说明：`path` 必须指向文件而非目录。验证脚本会检查 `exists + is_file`。

### 快速验证

在项目根目录下运行：

```python
import yaml
with open('applications/<app_name>/workflows/worker_agents/<worker_name>.yaml') as f:
    cfg = yaml.safe_load(f)
    print('name:', cfg.get('name'))
    print('schema:', 'agent_function_schema' in cfg)
```

---

## 2. 自定义工具导入失败

### 症状
```
ModuleNotFoundError: No module named 'applications.xxx.agent_tools.yyy'
ImportError: cannot import name 'zzz' from ...
```

### 排查步骤

| 检查项 | 说明 |
|--------|------|
| `module` 和 `function` 是否成对出现？ | 两者必须在 YAML 中同时存在 |
| `module` 路径使用点号分隔 | ✅ `applications.code_review.agent_tools.repo_context` |
| 模块文件是否存在？ | 确认对应的 `.py` 文件确实在磁盘上 |
| 函数名是否匹配？ | YAML 中的 `function` 必须与 `.py` 文件中的函数名完全一致 |
| 是否使用了装饰器？ | ❌ `@tool` 装饰器，✅ 普通函数 |
| 缺少 `__init__.py` | `agent_tools/` 目录通常不需要 `__init__.py`——框架通过动态导入加载 |
| 当前工作目录 | 必须在项目根目录下运行 |

### 快速验证

在项目根目录下运行：

```python
from applications.<app_name>.agent_tools.<module> import <function_name>
print(type(<function_name>))
print(<function_name>.__doc__[:100])
```

---

## 3. model_type 不存在

### 症状
```
ValueError: Model type 'xxx' is not defined in config/llm.yaml; the model call was not started.
ValueError: No model_type was provided and config/llm.yaml does not set `model.default_model_type`; the model call was not started.
```

### 排查步骤

1. 检查 `config/llm.yaml` 中是否定义了该 model_type
2. 从 `config/llm.yaml` 的 `model` 节点动态确认可用类型（排除 `default_model_type` 和非字典值）
3. 自定义 model_type 值必须先在 `config/llm.yaml` 中添加为配置块
4. 如果 Agent YAML 省略了 `model_type`，确认 `model.default_model_type` 已配置且指向可用类型

### 快速验证

在项目根目录下运行：

```python
import yaml
with open('config/llm.yaml') as f:
    cfg = yaml.safe_load(f)
model_cfg = cfg.get('model', {}) if isinstance(cfg, dict) else {}
available = [k for k in model_cfg if k != 'default_model_type' and isinstance(model_cfg[k], dict)]
print('default_model_type:', model_cfg.get('default_model_type'))
print('Available model_types:', available)
```

---

## 4. workflow 字段格式丢失

### 症状
Workflow 内容显示为单行，所有 Markdown 格式丢失。

### 原因
单个 `workflow` 未使用 YAML `|` 多行文本块语法，或顺序 `workflow` 列表项没有写成文本块。

### 修复
```yaml
# ❌ 错误
workflow: "# 标题\n## 步骤\n1. xxx"

# ❌ 错误
workflow:
  # 标题

# ✅ 正确：单个工作流
workflow: |
  # 标题

  ## 步骤
  1. xxx

# ✅ 正确：顺序工作流
workflow:
  - |
    # 第一段工作流
    ...
  - |
    # 第二段工作流
    ...
```

---

## 5. LLM 配置误写在 Agent YAML 中

### 症状
```
Warning: Field 'model'/'llm'/'langfuse' found in agent YAML, will be ignored.
```

### 原因
Agent YAML 中不应包含 `model`、`llm`、`langfuse` 等字段。这些配置统一在 `config/llm.yaml` 中管理。

### 修复
从 Agent YAML 中删除这些字段，使用 `model_type` 引用 `llm.yaml` 中定义的模型配置。

---

## 6. 入口脚本运行时错误

### 症状
```
ModuleNotFoundError: No module named 'src'
FileNotFoundError: ... agent.yaml not found
```

### 排查步骤

| 检查项 | 说明 |
|--------|------|
| 当前目录 | 必须在项目根目录下运行 |
| YAML 路径 | `_app.py` 中 `run_app(...)` 的路径必须与实际文件路径匹配 |
| sys.path | 确认入口脚本中 `project_root` 计算正确（目录层级） |

---

## 7. 验证脚本报错

### 症状
```
config/llm.yaml not found, unable to locate project root directory
```

### 修复

在项目根目录下运行（脚本会自动向上查找 `config/llm.yaml` 定位项目根目录）：

```bash
cd <project_root>
<python> <skill_root>/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>
```

---

## 8. agent_function_schema.inputs 参数名不合法

### 症状
验证脚本报错：`Input parameter name 'xxx' is not a valid Python identifier`

### 原因
`inputs` 下的键名必须是合法的 Python 标识符（以字母或下划线开头，仅包含字母、数字和下划线）。

### 常见错误
```yaml
# ❌ 包含连字符
inputs:
  file-path:
    description: "..."

# ❌ 以数字开头
inputs:
  1st_query:
    description: "..."

# ✅ 正确
inputs:
  file_path:
    description: "..."
  query:
    description: "..."
```

---

## 9. Skills 加载失败

### 症状
```
Warning: Skill directory not found: skills/xxx
Warning: Duplicate skill 'xxx' loaded, overwriting previous
```
或者运行时 Agent 未表现出预期的 Skill 行为。

### 排查步骤

| 检查项 | 说明 |
|--------|------|
| `skills` 路径不正确 | 路径相对于项目根目录（`C.agent_root`）解析。确认目录确实存在 |
| 缺少 `SKILL.md` 文件 | 每个 Skill 目录必须包含 `SKILL.md`（或 `skill.md`）才能被识别 |
| `invocation-control.allow-model` 值不合法 | 仅允许 `true`、`false` 或 `"force-inject"`。其他值会被忽略 |
| Skill 名称重复 | 如果同名 Skill 出现在多个层级（系统 / 目录 / Agent YAML），后加载的会覆盖先加载的（并输出警告） |
| `skills` 格式错误 | 必须是字符串、字典或列表之一。纯字符串列表有效；列表中混合字符串和字典也有效 |
| Skill 期望触发 Hook 但未触发 | 确认已设置 `invocation-control.allow-hook: true`。同时确认 Hook 脚本路径相对于 Skill 目录正确 |

### 三层加载顺序

```
第 1 层: config/system.yaml 全局 skills
第 2 层: <project_root>/skills/ 目录自动发现
第 3 层: Agent YAML 中的 skills 字段
```

各层是**叠加**关系（而非覆盖）。同名 Skill 后加载的会覆盖先加载的。

### 快速验证

检查 Skill 目录是否有效：

```bash
# 确认 Skill 目录包含 SKILL.md
ls <project_root>/skills/<skill_name>/SKILL.md

# 检查 YAML frontmatter
head -10 <project_root>/skills/<skill_name>/SKILL.md
```
