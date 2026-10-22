# 完整 Application 的 Harness 改进

在应用的 `config/system.yaml` 中启用后，一次完整任务会实际运行原方案、探索方案和组合方案。Supervisor 工作流与接入了完整任务驱动的程序应用使用同一个控制器；`Application.open()` 只打开生命周期，Worker 或 subagent 调用沿用所属 Trial。

```yaml
harnessing:
  enabled: true  # 默认 false；只在 Application 配置中显式启用。
  feedback:
    mode: contrastive  # contrastive：执行轨迹、产物和业务验收，不提供隐藏评分。
                       # development_score：额外使用明确指定的开发评价器及分区。
    # evaluator: applications.example.evaluation:Evaluator
    # partition: development-v1  # development_score 必须同时指定这两项。
  recursion:
    mode: parallel  # parallel：从共同基础探索；sequential：沿上一组合方案继续改进。
    k: 1            # 有限正整数。正常计划有 4k 次完整业务执行、3k 次改进会话。
  # editable 默认允许 system_prompt、skills、tools、toolsets、mcp_servers、worker_agents。
  # 可按需要缩小，例如 editable: [system_prompt, skills, worker_agents]。
  # roles 可引用普通应用内 Worker YAML；未指定时使用框架内置 Agent：
  # roles:
  #   general: workflows/worker_agents/improve_general.yaml
  #   domain: workflows/worker_agents/improve_domain.yaml
  #   compose: workflows/worker_agents/improve_compose.yaml
  #   select: workflows/worker_agents/select_final.yaml
  #   reflect: workflows/worker_agents/reflect.yaml
```

这些是 AgentLoom 的配置契约。通用改进、领域改进、Probe、Compose、有限递归、final 选择和反思借鉴 Harness²；开发评分、Application 配置与多 Agent 执行适配由 AgentLoom 提供。`k` 控制探索规模；选择、反思、评价和重试另计用量，现有单次调用超时和输出限制继续有效。

五个内置改进角色均使用 Pi，复用已安装的 Pi SDK 和现有认证；模型取应用生效的
`model.default_model_type`。业务 Worker 的 `agent_runtime` 与 `model_type` 仍由各自 YAML 声明。
应用无须重复定义五份角色 YAML；只有确实需要定制角色时才配置 `roles`。

内置角色可通过 Pi 的只读 `read` 工具读取本次隔离工作区。大型执行证据写入
`harness-evidence/original.json`，模型接收索引路径而不是把全部正文放进桥接输入帧。
索引保留全部字段；长文本按顺序分片，并记录字符数和 SHA-256，可完整还原。
角色使用 `read` 的 `offset`、`limit` 分页查看索引及所需正文。自定义角色继续接收原有
`evidence` 对象，工具与输入契约由其普通 Worker YAML 声明。
大型证据的输入同时提供冻结的 `resource_root`；逻辑资源路径相对此目录解析，
历史执行记录中的宿主机路径不能用来替代本次快照。

自定义角色使用与内置角色相同的输入输出契约：输入为包含 `evidence` 的对象；五种角色的输出如下。

| 角色 | 输出 |
| --- | --- |
| `general`、`domain`、`compose` | `{"edits": [{"path": "applications/example/prompts/task.md", "content": "完整的新文件内容"}], "rationale": "依据执行证据说明修改原因"}` |
| `select` | `{"chosen_id": "C1", "reason": "选择理由"}`；ID 必须来自给定 final 池。 |
| `reflect` | `{"experiences": [{"trigger": "触发条件", "symptom": "现象", "action": "行动", "verification": "验证证据", "evidence_refs": ["给定证据引用"]}]}`；证据不足时返回空数组。 |

`edits` 必须是数组；每项只包含 `path`、`content`，路径是项目内规范相对路径且不能重复。`content` 为完整 UTF-8 文本，`null` 表示删除允许删除的已有资源；不修改时使用 `edits: []`。框架先校验角色输出，再转换为内部文件修改映射，继续执行原有资源权限、凭据和整应用校验。所有输出对象都声明固定字段、完整 `required` 与 `additionalProperties: false`，符合 [OpenAI 严格结构化输出的对象约束](https://developers.openai.com/api/docs/guides/structured-outputs)；自定义 YAML 应复用对应内置角色的 `output_schema`。不接受旧的动态路径字典格式。

## 执行与恢复

普通 Supervisor 入口会读取应用开关：

```sh
loom run applications/example/workflows/root.yaml
loom run applications/example/workflows/root.yaml --output-format json
loom run applications/example/workflows/root.yaml --resume-experiment experiment_ID
```

文本输出给出实际交付内容和实验 ID。JSON 输出只给出选中 Run 的完成事件，并附带 `harnessing` 实验回执；JSONL 保留各 Trial 的实际 Run 事件。实验失败或停止时，机器输出提供实验状态，退出码分别为 1 或 130。原有 `--resume` 用于普通任务检查点；实验使用 `--resume-experiment`，二者不能同时指定。

Python 入口返回相同的选中 Run 和完整实验：

```python
from agentloom.app.runner import execute_app

result = execute_app("applications/example/workflows/root.yaml")
print(result.output)
print(result.harnessing.experiment_id)
# 恢复复用已归档候选、已完成业务、选择与发布回执：
recovered = execute_app(
    "applications/example/workflows/root.yaml",
    harness_resume_id=result.harnessing.experiment_id,
)
```

程序应用接入 `agentloom.app.harnessing.controller.run_application`，提供一次性的完整任务驱动。驱动负责准备固定事实、整体验收、恢复核对和幂等发布；不能只在改进器外循环调用某个 Worker。公开请求支持显式事实输入与隐藏数据路径，模型配置和凭据由可信父进程绑定。

需要在首次模型执行前保存任务与实验对应关系的调用方，可先持久化自己的实验 ID，首次调用时传入
`experiment_id`，之后用 `resume_id` 恢复。两者不能同时传入；恢复读取已归档策略。

## 结果含义

只有实际执行且通过业务验收的组合 final 能进入选择池。`k=1` 直接采用唯一合格 final，`k>1` 调用选择 Agent；没有合格 final 或选择失败时明确失败。Compose 可以保留基础配置，但该配置仍须重新完整执行才能成为 final。

`completed` 表示本次执行、交付及后续步骤完成。交付后反思失败会保留交付并报告 `partial`。采用表示选中了本次交付内容；是否优于原方案，需要另外检查比较证据。后续任务从应用声明的基础配置开始；有证据的反思通过现有 Curated Memory 审核流程提交，经验库只作为改进器的参考。

实验回执区分 Task、候选、Trial、实际 Run 和调用尝试，保留已知 token、耗时、失败与缺报。累计 usage 按调用和尝试对账；没有计量证据的字段保持未知。恢复先核对既有进程和业务回执，业务完成后用量缺报不会触发业务重跑。

恢复使用归档的应用策略、项目默认配置、资源和事实。未完成任务只按既有私有引用重新绑定当前凭据；已完成任务核验归档后直接复用回执。默认入口仍从项目运行配置定位实验目录；该定位配置损坏或位置变化时，程序入口须显式提供原 `state_root`，无需新增实验索引。

## 隔离与运行环境

当前启用能力要求 Linux Landlock ABI 6+ 与 libseccomp。运行前检测真实能力；未验证的平台明确拒绝启用。普通应用继续使用既有执行方式。

候选固定资源与框架代码只读；每个 Trial 分离业务状态、Runtime、产物和会话。新闻程序的可信编排在外层受管进程执行，各实际 NewsHost 在加载 Agent、模型或工具前强制安装同一 Trial 的访问限制。完整物理退出由外层受管资源回执确认。

内容相同的候选共享不可变快照。并行提案同时提交相同快照时，框架核验并复用已提交的版本，
不因此重新调用改进模型；各候选的业务试跑仍独立执行。

凭据正文不写入候选或实验文件。 绑定凭据若被模型响应、工具结果或 MCP 工具发现信息回显，会在运行记录、SDK 会话和候选写入前拒绝；只保留安全错误及已报告的数值用量。工具已经执行时保留失败或效果不确定的回执，停止当前执行，不能把拒绝当作可自动重试的未执行操作。模型、MCP 和 Pi 订阅登录通过匿名通道传递并在工具创建前消费关闭。Pi 的刷新权限留在可信父进程的原有 SDK 登录存储中；隔离进程使用临时访问令牌。令牌过期时需要由可信父进程重新绑定后恢复。

`.env` 及 `.env.*` 文件不进入候选资源。stdio MCP 的完整 `args` 作为可信父进程绑定的私有能力，避免位置参数或任意参数名携带的令牌泄漏；改进器可以调整服务器引用，不能修改或重定向已绑定的服务器启动参数。父进程的 Pi 登录刷新同样由既有原生进程 guardian 管理，父进程被强制结束后仍须回收刷新子进程。

本地隔离覆盖文件内容、执行工具、继承的子进程以及本地 stdio MCP。远端 MCP 的服务能力需要由应用单独限制；本地 Landlock 不改变远端服务器的权限。该机制也不承诺隐藏所有文件存在性元数据，或回放实时网络。外部资料变化、历史采集完整性和独立未见评分仍须分别记录。
