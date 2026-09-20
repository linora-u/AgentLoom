# 08 实施与验证记录

状态：实施中，尚未完成验收。

- 分支：`codex/pi-t08-platform`。
- 起始提交：`9d5a88915efbd402d0f2697318125933fc1500e0`，包含 03 冻结提交。
- 独立 worktree：`/Users/bytedance/.codex/worktrees/pi-t08-platform/AgentLoom`。
- 已私密复制 `config/llm.yaml`，权限为 0600，Git 忽略；报告不包含配置内容。
- 采用主工作区最新票据约束，专业工具按需启动必须在 Application 入口验证。

## 1. 平台 Goal 脱离 smol 工具构造

Goal 工具改为普通有类型函数，由既有中立工具绑定推导 schema；root Supervisor 权限、完成证据和已有状态语义保留。

新增子进程验收禁止加载 smol SDK/adapter，先复现 Goal 构造失败，再通过中立绑定构造验证。已有 Goal 行为用例使用函数公开入口，原 smol Application 回归保留。

验证命令：

```sh
.venv/bin/python -m pytest tests/goal_test tests/application_test/test_smol_compatibility_application.py -q --tb=short
```

结果：56 passed，2 项既有第三方弃用警告。此轮为确定性验证，真实模型 Application 验证尚待后续执行。
