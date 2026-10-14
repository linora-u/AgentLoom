# 进程管理简化：决策树与待验证问题

2026-10-14。应用 `grill-with-docs` 的 grilling 与 domain-modeling 指令；本轮只读代码、记录讨论，不修改产品、测试、配置或运行任务。OpenClaw 原型尚未提交、推送或合入。部署与故障合同见 [ADR-0003](../adr/0003-library-owned-cross-platform-process-lifetime.md)。

## 已确认，不重复询问

- 默认本地持久化，预留远程存储；首版单机多进程，macOS/Linux 无需用户配置系统服务。
- 顶层执行 Python 被强杀后，允许库的独立监护继续存活，必须回收任务本机后代并退出；任务不得主动逃离管理范围。
- 初筛、精筛、校验共享阶段容量 20/30/20；有限 map 上限属于单次调用。
- 新闻筛选、校验、计分和既有业务失败恢复规则保留。
- 真实业务验收使用完整日期、DeepSeek v4.1、AnySearch；本地 HTTP fixture 不能替代该验收。
- 用户允许固定上游版本复用原实现，但当前要求先讨论，不继续修改代码。
- 用户随后确认默认装配：Agent/Worker 只配置并发数量，由 AgentLoom 使用统一受管执行机制；不提供 `process_owner: openclaw` 选择项。插件仅是实现的组织方式。

## 影响选型的事实

1. 当前 OpenClaw Adapter、Node 桥和 installer 共 263 行；官方 npm 发布包约 103MB，还需要 Node。直接调用 SDK 没有消除跨语言接线和回执维护成本。[直接复用核对](openclaw-direct-reuse-scope-20261014.md)
2. 完整 NodeWorkerSupervisor 自带 journal、claim、capacity 和 workspace；整体引入会与 DBOS 重叠。底层 owned-stdio 是通用命令 owner 候选，但未公开导出，且普通命令与其专有 Worker 的 root 退出语义不同。不能把内部入口名称当成更短实现的证明。[固定源码依据](openclaw-direct-reuse-scope-20261014.md)
3. 父死亡监护直接观察的是 Node 调用宿主；Python 在上层时仍需要生命周期关联。把 Node 改为外层入口是另一项架构选择，尚未验证整体接入会更少。

## 新发现，按证据强度记录

**清理失败可能无限等待（源码事实；uncertain 分支未故障注入）。** 当前没有发现立即释放本机额度的路径：cleanup 失败后持续等待有效资源退出回执，之后才移除 active 并释放 scope lock。OpenClaw 缺少有效回执时，等待可以一直持续，executor 收尾也会等待 active 清空。需要明确可见的阻塞状态、控制命令的返回行为和后续确认途径，不能用超时假称资源已退出。见 [调用收尾](../../src/execution/durable/os_job.py#L228) 与 [executor 收尾](../../src/execution/durable/executor.py#L112)。固定 DBOS 3.2.0 的本机 worker_concurrency 根据 active workflow 计算，preemptible cancellation 等待 step 收尾；数据库 CANCELLED 本身不等于该额度已经释放。

**启动授权与撤销存在可构造竞态（源码推断，尚未实跑）。** 子进程看到 authorized.json 后才创建业务 grant。若父进程先 revoke，业务库尚无该 grant，撤销更新会没有匹配项；父仍记录 revoked.json。子随后可能创建 grant，恢复又可能因 revoked.json 跳过再次撤销，留下已死 invocation 的权限或短暂重新获得写权。物理退出保护仍在，不能据此声称同任务已发生并跑。需要验证“取消先到，则该尝试以后也不能取得授权”。见 [启动入口](../../src/execution/durable/job_entry.py#L24)、[grant](../../applications/news_agent/runtime/store.py#L288)、[revoke](../../applications/news_agent/runtime/store.py#L342)、[恢复](../../src/execution/durable/os_job.py#L128)。

**本机清理与远端请求是两个事实。** 本机 Pi/MCP/工具退出不能证明 Provider 已停止推理或计费；恢复重发可能带来额外模型/搜索用量。当前 20/30/20 是本机 Worker 容量合同，不能外推为远端推理严格不重叠。已完成的业务通过完成回执恢复，无法证明停止的请求应如实记录，不能补零或宣称 exactly-once。

**验收范围仍有限。** Linux 原型有 7 项 Application、2 项真实 Pi/MCP 和 3 种故障探针；后续 macOS 默认入口/controller/CLI 30 项和真实 Pi/MCP 2 项通过。尚未验证新 owner 的真实 News 全日链路、uncertain 故障注入或上面的授权竞态。旧 launchd 全日结果不能替代新 owner 的验收。见 [Linux 记录](../reviews/openclaw-linux-process-owner-20261014.md)。

## 本轮用户决定

- Q8：用户回答“直接借鉴 openclaw 就行了”。参考来源固定为 OpenClaw，不继续开展开放选库。借鉴机制不等于已确认依赖完整 SDK、复制完整产品或引入第二套调度器；接入形态仍须在方案中说明。
- Q9：用户回答“参考 openclaw 机制来啊，别自己想乱七八糟的东西啊”。清理异常按上游已有行为对照，不另行发明阻塞策略。

## 按上游原机制对照

1. **取消先关闭启动资格。** 上游同步 abort admission/turn，授权事务内再次 assertCurrent；持久终态不会被 markRunning 覆盖。登记进程身份后、开 start gate 前和发送业务输入前继续核对。当前原型“先撤销、后创建 grant”的竞态须据此对齐，不能只靠多加一份文件判断。
2. **清理不确定保留 owner 与容量，控制请求可报告失败。** 上游把 uncertain 结果保留为 deferred outcome，status/stop 再确认资源退出，无法确认时明确报错。当前原型无限 await 退出文件，与完整上游机制不一致。
3. **控制请求与后台观察分开。** 上游 recovery 默认观察 1 秒后返回已有回执，后台继续保留清理观察；调用方显式要求 awaitCleanup 时才完整 join。close 仍 join 已接受工作并汇总错误，不能承诺统一固定的关闭总耗时。
4. **物理退出与终态持久化成功后才释放容量。** 缺少可信退出事实时，上游本身也保留容量，不能用重启或超时代替退出证明。

精确源码和测试引用见 [完整上游行为核对](openclaw-direct-reuse-scope-20261014.md)。本轮没有复现授权竞态、运行 uncertain 探针或修改产品；以上是源码对照，不是修复完成报告。

接下来只需把最小接入方案逐项对应到上游机制，列出可删除的现有代码、不可省的 Python 接线和剩余验收。新的具体实现尚未形成共同确认，不据此继续开发。
