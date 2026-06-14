# Application 生成规范

## 标准目录

```
applications/<app_name>/
├── README.md
├── <app_name>_app.py
├── agent_tools/
│   └── <tool_module>.py
├── skills/
│   └── <skill_name>/
│       └── SKILL.md
├── config/
│   └── system.yaml
└── workflows/
    ├── <app_name>_agent.yaml
    └── worker_agents/
        └── <worker>.yaml
```

`workflows/` 是 Application 标识；`agent_tools/` 只在需要确定性工具时创建；`skills/` 只在需要应用私有 Skill 时创建；`config/` 只在需要应用级配置时创建，例如把 GitHub skill 目标或其他验证参数放在 `config/system.yaml`。

## 文件生成顺序

1. `workflows/<app_name>_agent.yaml`
2. `workflows/worker_agents/*.yaml`
3. `skills/<skill_name>/SKILL.md` 与必要的 `references/`、`scripts/`、`assets/`
4. `agent_tools/*.py`
5. `config/system.yaml`
6. `<app_name>_app.py`
7. `README.md`

没有对应需求时不要创建空目录。应用专属的 skill 和 skill 运行目标应放在 Application 下面，不要放到全局 `skills/` 或额外的孤立配置文件里。

## 入口脚本模板

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


def main(user_request: str, log_to_file: bool = False, resume: str | None = None) -> str:
    if not user_request or not user_request.strip():
        raise ValueError("user_request must be non-empty")

    task = f"用户需求:\\n{user_request.strip()}\\n\\n请按 workflow 执行。"
    result = run_app(
        "applications/<app_name>/workflows/<app_name>_agent.yaml",
        task_override=task,
        log_to_file=log_to_file,
        resume_task_id=resume,
    )
    print(result)
    return result


if __name__ == "__main__":
    fire.Fire(main)
```

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

## README 必写内容

- 这个 Application 解决什么问题。
- 输入参数是什么。
- 输出是什么。
- Supervisor/Worker/Tool 分工。
- 如何运行。
- 验证命令和当前验证结果。
- 已知问题和未执行的验证。
