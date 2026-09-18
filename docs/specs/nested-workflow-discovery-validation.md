# 嵌套 workflow 定义发现：修复与验证

跟踪问题：[GitHub #69](https://github.com/linora-u/AgentLoom/issues/69)。
基线：`origin/main` 的 `25f8eaf0e8179d560fab7815af7d7fa1735b4dfd`。

## 修复

`agentloom.application.definition` 提供唯一的只读定义发现器。它递归扫描
`workflows/` 下的 YAML、YML 和 Markdown 文件，不跟随 symlink；相对路径中包含
`worker_agents` 目录段的文件按 Worker 分类，其余文件按 Supervisor 分类。

框架 Skill 的结构扫描和 YAML 校验都复用该发现器。YAML 校验将发现角色传给共享
定义预检，所以未被 Supervisor 引用的嵌套 Worker 仍执行 Worker 专属校验，嵌套
Supervisor 也会检查完整 Worker 引用图。

## 确定性验证

| 检查 | 结果 |
|---|---|
| Skill 验证器、结构扫描器、共享定义与 Studio 相关回归 | **192 passed** |
| Python 全量 CI 命令，含两份 memory campaign 合同文件 | **4068 passed，2 skipped，0 failures/errors** |
| Ruff 与 `git diff --check` | 通过 |

新增回归覆盖：

- 只有嵌套 Supervisor 和嵌套 Worker 的合法 Application；
- 顶层合法 Supervisor 不能掩盖嵌套非法 Supervisor；
- 未引用的嵌套 Worker 继续执行 Worker 专属规则；
- 任意 `worker_agents` 路径段之下的定义都按 Worker 分类；
- symlink 文件、目录以及 `workflows/` 根目录不会进入扫描结果。

两个既有 skip 分别是当前机器没有 Docker CLI 的真实 Docker 测试，以及 Windows
专用 wexpect 测试。

## 真实 Application

仓库包含可复跑的
`applications/nested_workflow_discovery_validation/workflows/groups/review/supervisor.yaml`，
其 Worker 位于同一嵌套目录的 `worker_agents/` 下。Supervisor 与 Worker 的
SHA-256 分别为 `dc6bd551357c13756fe06c8a152670163726cdcf566a41931853f92b9d89c729`
和 `853b8beaf038d897ee3eee911ff4886d9abd3558ae814935d223a1d9ec8638cc`。
三种静态入口先对同一份定义给出一致结果：

- Skill YAML 校验：`valid=true`，`files_checked=2`；
- Skill 结构扫描：1 个 Supervisor、1 个 Worker；
- 领域 `application.validate`：`valid=true`。

随后使用独立 `AGENTLOOM_RUNTIME_ROOT` 和本地 `powerful` 模型配置执行真实
`loom run`：

- task：`task_20260918T035736159774Z_46bd87f48b38`
- Run：`run_20260918T035736159795Z_21a683b33a7a`
- manifest：`status=completed`
- Worker 事件：一对 `worker_call_started` / `worker_call_finished`，
  `call_index=0`，结果为 `ISSUE_69_NESTED_WORKER_PASS`
- 最终结果：
  `ISSUE_69_NESTED_APPLICATION_PASS worker=ISSUE_69_NESTED_WORKER_PASS`

第一次尝试使用本地 `fast` 模型配置时，LiteLLM 在模型请求阶段拒绝 Gemini 自定义
地址的 context-cache 参数，尚未调用 Worker；该 Run 被中断并保留在隔离 runtime。
随后先用临时定义从全新 runtime 验证 `powerful` 路由，再将相同定义固化为上述
正式 Application，并从另一个全新 runtime 重跑成功；最终记录没有复用前两次状态。
