# Application 生成规范

## 标准目录

```
applications/<app_name>/
├── README.md
├── <app_name>_app.py                 # 可选：需要自定义 CLI/task_override 时才创建
├── agent_tools/
│   └── <tool_module>.py
├── skills/
│   └── <skill_name>/
│       └── SKILL.md
├── hooks/
│   └── <hook_name>/
│       ├── HOOK.yaml
│       └── scripts/
├── config/
│   └── system.yaml
└── workflows/
    ├── <app_name>_agent.yaml
    └── worker_agents/
        └── <worker>.yaml
```

`workflows/` 是 Application 标识；`agent_tools/` 只在需要确定性工具时创建；`skills/` 只存放应用私有 Skill；`hooks/` 只存放显式引用的独立 Hook Bundle；`config/` 只在需要应用级系统配置时创建。

不要在 Application 目录里新建 `llm.yaml`。模型路由、密钥、温度、重试、限流等只写全局本地 `config/llm.yaml`，Agent 通过 `model_type` 选择。

## 文件生成顺序

1. `workflows/<app_name>_agent.yaml`
2. `workflows/worker_agents/*.yaml`

如果用户要求长期自动推进目标，可在第 1 个顶层 Supervisor YAML 配置 Goal Mode。
推荐 `goal: {enabled: true, token_budget: <正整数>}`，无人值守或 schedule 场景必须
主动讨论预算；省略预算意味着无限 continuation。Worker YAML 一律不写 `goal`。
3. `skills/<skill_name>/SKILL.md` 与必要资源
4. `hooks/<hook_name>/HOOK.yaml` 与脚本（仅需要 Hook 时）
5. `agent_tools/*.py`
6. `config/system.yaml`
7. `<app_name>_app.py`（可选；只有需要自定义 CLI 参数、预处理/后处理、批处理或 `task_override` 时创建）
8. `README.md`

没有对应需求时不要创建空目录。应用专属 Skill 与 Hook Bundle 分别放在 Application 的 `skills/`、`hooks/` 下，并在配置中分别启用。

## 入口脚本模板

默认优先使用直接 YAML 入口：

```bash
.venv/bin/loom run applications/<app_name>/workflows/<app_name>_agent.yaml
```

`<app_name>_app.py` 不是 Application 必需文件。只有当应用需要自定义自然语言请求、预处理/后处理、批量循环、`file_logging`/`resume` 包装，或需要通过 `run_app(..., task_override=...)` 嵌入到别的 Python 流程时，才创建入口脚本。

```python
#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

import fire

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.runner import run_app


def main(user_request: str, file_logging: bool | None = None, resume: str | None = None) -> str:
    if not user_request or not user_request.strip():
        raise ValueError("user_request must be non-empty")

    task = f"用户需求:\\n{user_request.strip()}\\n\\n请按 workflow 执行。"
    result = run_app(
        "applications/<app_name>/workflows/<app_name>_agent.yaml",
        task_override=task,
        file_logging=file_logging,
        resume_task_id=resume,
    )
    print(result)
    return result


if __name__ == "__main__":
    fire.Fire(main)
```

`file_logging=None` 表示遵循全局 `logging.file_enabled`，`False` 表示仅关闭当前 attempt 的文件 runtime log；checkpoint 和 Shell audit 不受影响。不要保留旧 `log_to_file` 参数兼容。

## Tool 生成原则

- Tool 是普通 Python 函数，不需要装饰器。
- 函数名就是 YAML 中的 `function`。
- docstring 是工具说明来源，必须写清输入输出。
- 不要让 LLM 做文件遍历、CSV 解析、JSON 校验、批量循环、缓存写入这类确定性工作。

## Skill 生成原则

- Skill 包使用 `skills/<skill_name>/SKILL.md`，文件名按 `SKILL.md` 书写。
- `SKILL.md` frontmatter 至少写 `name` 和 `description`；需要模型传参时写 `argument-hint`。
- 参考资料放 `references/`，脚本放 `scripts/`，模板或静态素材放 `assets/`。
- Agent YAML 只配置要注册的 skill 路径和加载策略；不要在 YAML 里维护 source、commit、hash、license 这类审计元数据。
- 第三方脚本默认允许执行；只有用户明确要求限制时，才配置 `allow-scripts: false` 或 `allow-network: false`。
- 如果某个 skill 只服务当前 Application，放在 `applications/<app_name>/skills/`；确实跨应用复用时，再考虑全局 runtime skill。
- `SKILL.md` 不得包含 `hooks`；确定性事件行为使用独立 `hooks/<name>/HOOK.yaml` Bundle，并通过 system/Agent YAML 顶层 `hooks.bundles` 显式引用。

## 应用级 config/system.yaml 原则

只有需要覆盖当前 Application 的系统行为时才创建：

```yaml
skills: []                 # 关闭全局 skill 列表和 AGENT_ROOT/skills 自动发现
hooks:
  bundles:
    my-hook:
      path: applications/<app_name>/hooks/my-hook
toolsets: []   # 纯规划 Agent 可显式关闭内置工具
tool_access_control:
  path_validation:
    - tools: ["read_file", "grep_search", "shell_tool"]
      include_paths: ["/absolute/allowed/path"]
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  audit_log:
    enabled: true
    log_policy_snapshot: true
    log_success: false
mcp_servers:
  path: "applications/<app_name>/config/.mcp.json"
```

列表是整体替换，不是追加；写 Agent 级 `toolsets`、`skills` 这类列表时要表达完整意图。完整配置面见 `configuration-surface.md`。

`allowed_commands: "*"` 与 `allowed_operators: "*"` 是全放开，只适合可信开发环境或先观察 audit 的探索阶段。用户不确定权限时，先运行真实 workflow，读取当前 run 的 `manifest.json` 和 `audit/shell.jsonl`，再根据 `[POLICY_SNAPSHOT]` 与拦截事件收敛命令、操作符、路径或 sandbox 配置。

## README 必写内容

- 这个 Application 解决什么问题。
- 输入参数是什么。
- 输出是什么。
- Supervisor/Worker/Tool 分工。
- 如何运行。
- 验证命令和当前验证结果。
- 已知问题和未执行的验证。
