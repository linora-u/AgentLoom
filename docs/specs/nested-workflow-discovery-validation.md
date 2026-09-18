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
| Skill 验证器、结构扫描器、共享定义与 Studio 相关回归 | **227 passed** |
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

## 真实 Application 验证

所有真实 Run 都使用 worktree 中受 Git 忽略的本地 LLM 配置和独立
`AGENTLOOM_RUNTIME_ROOT`。每条命令均返回退出码 0；随后分别检查 CLI JSONL、
manifest、`artifacts/result.txt`、`audit/task_events.jsonl` 与
`logs/runtime.log`，而不是只依赖进程退出码。三条 workflow 都未调用 Shell，
所以按需生成的 `audit/shell.jsonl` 均不存在。

### 嵌套 workflow 发现

仓库新增可复跑的
`applications/nested_workflow_discovery_validation/workflows/groups/review/supervisor.yaml`，
其 Worker 位于同一嵌套目录的 `worker_agents/` 下。Supervisor 与 Worker 的
SHA-256 分别为 `dc6bd551357c13756fe06c8a152670163726cdcf566a41931853f92b9d89c729`
和 `853b8beaf038d897ee3eee911ff4886d9abd3558ae814935d223a1d9ec8638cc`。
三种静态入口先对同一份定义给出一致结果：

- Skill YAML 校验：`valid=true`，`files_checked=2`；
- Skill 结构扫描：1 个 Supervisor、1 个 Worker；
- 领域 `application.validate`：`valid=true`。

真实运行命令：

```sh
AGENTLOOM_RUNTIME_ROOT=/private/tmp/agentloom-issue-69-runtime-final \
  .venv/bin/loom run \
  applications/nested_workflow_discovery_validation/workflows/groups/review/supervisor.yaml \
  --output-format jsonl
```

- 退出码：0
- task：`task_20260918T035736159774Z_46bd87f48b38`
- run：`run_20260918T035736159795Z_21a683b33a7a`
- manifest：`status=completed`
- Worker `nested_discovery_acceptance_worker`：一对 `worker_call_started` /
  `worker_call_finished`，`call_index=0`，`status=completed`，结果为
  `ISSUE_69_NESTED_WORKER_PASS`
- 最终结果：
  `ISSUE_69_NESTED_APPLICATION_PASS worker=ISSUE_69_NESTED_WORKER_PASS`
- runtime log：没有框架级 `[ERROR]` 或 `[WARNING]`
- Shell audit：未生成，因为该 workflow 没有调用 Shell
- CLI JSONL：`/private/tmp/agentloom-issue-69-final.jsonl`

### Markdown Tool Registry

真实运行命令：

```sh
AGENTLOOM_RUNTIME_ROOT=/private/tmp/agentloom-issue-69-runtime-markdown \
  .venv/bin/loom run \
  applications/tool_registry_markdown_validation/workflows/markdown_report_agent.yaml \
  --output-format jsonl
```

- 退出码：0
- task：`task_20260918T062131399590Z_6c027c7b20de`
- run：`run_20260918T062131399631Z_01035827abfb`
- manifest：`status=completed`
- 工具行为：实际加载并调用 `write_markdown_file` 与 `read_file`
- 最终结果：`MARKDOWN_TOOLSET_VALIDATION: PASS`
- 输出文件：`/tmp/agentloom_tool_registry_markdown_validation/report.md`
- 输出 SHA-256：
  `4ba0b8f136725ab44319797134aa3e717b403846555c6936bbff99ef59c18ee5`
- runtime log：没有框架级 `[ERROR]` 或 `[WARNING]`
- Shell audit：未生成，因为该 workflow 没有调用 Shell
- CLI JSONL：`/private/tmp/agentloom-issue-69-markdown.jsonl`

### Multi-Worker Context

真实运行命令：

```sh
AGENTLOOM_RUNTIME_ROOT=/private/tmp/agentloom-issue-69-runtime-multi \
  .venv/bin/loom run \
  applications/context_engine_multi_worker_validation/workflows/context_engine_multi_worker_validation_agent.yaml \
  --output-format jsonl
```

- 退出码：0
- task：`task_20260918T062131407594Z_6d72307dad66`
- run：`run_20260918T062131407629Z_fc07b23de958`
- manifest：`status=completed`
- Worker `log_payload_worker` 与 `search_payload_worker`：均为
  `call_index=0`、`status=completed`
- 工具行为：实际多次调用 `loom_retrieve_context`
- 最终结果：
  `MULTI_CONTEXT_RETRIEVE_PASS log_value=LOG-CTX-8842 search_value=SEARCH-CTX-6194`
- runtime log：没有框架级 `[ERROR]` 或 `[WARNING]`；日志文本中的
  `ERROR case=log failure=synthetic-but-preserved` 与 `Traceback` 是该
  Application 专门生成并验证保留行为的测试载荷
- Shell audit：未生成，因为该 workflow 没有调用 Shell
- CLI JSONL：`/private/tmp/agentloom-issue-69-multi.jsonl`

第一次尝试使用本地 `fast` 模型配置时，LiteLLM 在模型请求阶段拒绝 Gemini 自定义
地址的 context-cache 参数，尚未调用 Worker；该 Run 被中断并保留在隔离 runtime。
随后先用临时定义从全新 runtime 验证 `powerful` 路由，再将相同定义固化为上述
正式 Application，并从另一个全新 runtime 重跑成功；最终记录没有复用前两次状态。
