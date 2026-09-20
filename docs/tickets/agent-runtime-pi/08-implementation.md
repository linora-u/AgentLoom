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

## 2. 中立 MCP 工具与连接生命周期

发现的 MCP 工具直接成为 ToolBinding，保留完整 JSON Schema、外部提供方、历史可见工具名与结构化结果。文本 JSON 保持原有解释，多内容结果保留全部协议块；媒体内容以中立 MCP 块返回，不引入 smol 媒体类型。错误结果继续保留 mcp_error 分类。远程 schema 引用不触发自动网络读取。

真实 stdio 服务复现了旧同步桥接的两个问题：重复 connect 尝试重启同一线程；关闭连接后等待工具结果的线程可能永远不结束。连接现由独立 AnyIO portal 持有，显式追踪并取消待处理调用，关闭等待传输和进程释放。重复连接/关闭、重新连接、旧绑定失效、双连接隔离、连接超时及调用中取消均使用真实服务验证。

共享接线仅修改 factory 的 MCP 装配部分：识别中立绑定的可见名称，发现名称冲突时关闭已连接服务再失败。独立用例通过两个真实 MCP 服务验证重复名称拒绝及清理，不修改 05 的 Gateway 实现或公共运行合同。

```sh
.venv/bin/python -m pytest tests/mcp_test -q --tb=short
uv tool run --from mypy==2.3.1 mypy --follow-imports=silent src/adapters/mcp src/tools/goal
```

结果：83 passed；8 个源文件类型检查通过。真实模型应用、无 smol 环境、专业工具生命周期和完整回归尚待后续验证。
