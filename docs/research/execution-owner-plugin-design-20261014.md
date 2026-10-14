# 进程管理插件：设计草案

2026-10-14。用户希望参考 OpenClaw，将进程管理作为 AgentLoom 插件加载，并询问是否必须等待下一版本。用户随后明确：不提供 `process_owner: openclaw` 选择项，只要配置并发，就默认采用 AgentLoom 的受管执行机制。本轮只讨论、阅读源码和记录方案，没有修改产品、测试、配置或运行任务。以下接口和装配方式均为拟议设计，尚未实现或发布。

## 推荐

把进程管理封装为内置默认执行插件，由框架自动装配。用户只配置现有并发数量，不选择 owner 或参考项目名称。AgentLoom 定义一个小而明确的 Interface，插件在它后面复用已核对的 OpenClaw 机制；持久任务继续交 DBOS 管队列与容量，NewsAgent 保留业务授权和完成回执。插件不重新领取任务、维护业务重试或增加第二套容量调度。

不需要等待 AgentLoom 下一大版本或 OpenClaw 新版公开接口。当前开发周期可以增加兼容接口和默认装配，发行时准备好所需执行依赖；旧运行迁移和旧入口移除另行安排。不能把插件封装等同于物理退出、取消竞态已修复或依赖体积已下降。

## 为什么这个 seam 存在

当前 `DurableApplication.executor(..., jobs=...)` 已支持可信依赖注入；原 launchd 和新 OpenClaw 是两个真实 Adapter。问题在于 `DurableExecutor` 默认直接导入 OpenClawJobs，读取实现的 root/host/active 并设置 stop_token；NewsAgent.stop_executor 也直接实例化 OpenClawJobs。`OwnedJobs` 还混有业务 reconcile/revoke、重放决策和结果处理，不能直接搬到插件并称它是纯进程管理。

现有 AgentRuntime/RuntimeRegistry 处理 Pi/smolagents 的 Agent 执行能力，不应同时承担持久任务资源所有权。该插件单独装配，避免把进程管理插件和模型运行时插件混成一个概念。[现有 executor](../../src/execution/durable/executor.py)、[现有注入入口](../../src/execution/durable/client.py#L146)、[Runtime 装配](../../src/app/composition.py#L50)。

## 三种设计比较

| 设计 | Interface 与收益 | 代价 |
| --- | --- | --- |
| 仅进程 owner 插件 | 在 executor 与执行尝试资源之间放 seam；隐藏上游 owner、Node 关联、退出确认与资源记录 | 必须把业务授权/重放与进程实现分开；不能只继承现有 OwnedJobs |
| 完整持久 ExecutionBackend 插件 | Application.durable 后面隐藏 DBOS 和进程管理，核心更少发行依赖 | `.client`、DBOS handle、HostSpec 模块路径等已有耦合使迁移面更大；目前只有一个持久引擎，不急于新增通用 engine 平台 |
| 配置装配 owner | 普通调用者不传具体类，启动、停止和恢复共用同一加载路径 | 配置只是装配入口，仍需要第一种设计的生命周期合同 |

采用第一种的实现隔离。第三种仅保留“普通调用者不接触具体类”，取消用户选择 owner 的配置；框架内部默认装配。初次实现优先内置模块，不为只有一个默认实现另造通用插件发现平台；未来是否独立发包不影响用户配置。完整 backend 插件可留待存在第二个真实持久后端时再评估。

## 职责

| Module | 负责 |
| --- | --- |
| AgentLoom 共享执行层 | Application/Worker/WorkItem、执行尝试身份、可信宿主、启动授权与取消的共同合同、trace 关联、检查资源退出后才允许补位 |
| DBOS 接入 | SQLite 持久任务、事务提交、共享 20/30/20 阶段容量和 workflow 恢复 |
| 进程管理插件 | 完整 Application 的本机资源所有权、父死亡关联、原机制的启动闸门/停止/退出观察、具体资源描述、平台差异、上游依赖安装与版本兼容 |
| NewsAgent | 新闻阶段衔接、业务写权限的事务实现、筛选/校验/封存/计分及业务完成回执 |

共享的受管理进程作用域仍需由框架提供给 Pi/MCP/工具。插件提供可信的作用域绑定，框架验证并传播；不能只移动文件就认为所有 Runtime 已自动受管。插件不直接实现 News grant，也不通过另建 claim/lease/journal 改写 DBOS 调度。

## 最小 Interface 与不可省合同

普通调用者仅面对 Application、BoundWorker 和 DurableApplication，不接触原生 SDK 或插件对象。并发执行默认使用统一的受管生命周期 Interface；插件作者面对的主要能力为：

1. 打开执行域：预检、读取旧资源事实并恢复收尾，返回可用 owner 会话；成功后才启动 DBOS 消费。
2. 执行一次尝试：接收由框架创建的可信启动描述与启动许可；返回业务传递结果与独立的资源退出事实。物理完成等待不因控制请求报错而放弃。
3. 检查与停止：控制请求可按上游机制返回清理未确认；会话保留其资源所有权，框架保留相应容量。会话关闭停止接纳并收尾已接受的资源。

具体类型和方法名应在最小原型中确定，不把整套 OpenClaw descriptor/claim 作为框架公共 Interface。框架不读插件 PID 表、Node IPC 或文件布局，也不根据进程结果直接推断后代消失。

授权与取消必须对同一已登记的执行尝试协调；开启动闸门前复核取消，已取消状态不得被后到的授权重新激活。这需要框架与业务 grant 事务的共同合同，插件化自身不会修复当前静态检查发现的竞态。清理失败按上游已有的 deferred 观察和容量保留机制对照，不自行发明一套异常调度。见 [上游机制](openclaw-direct-reuse-scope-20261014.md)。

## 装配与依赖

用户只保留已有并发配置，例如一个 Application 的默认上限：

```yaml
worker_execution:
  defaults:
    concurrency: 20
```

NewsAgent 继续使用现有三个 Worker 的 20/30/20 声明。无论使用默认数量、Worker 覆盖还是 `map(concurrency=N)`，都不新增 owner 参数。

普通持久调用保留：

```python
with app.durable() as execution:
    execution.executor(workers, host=host_spec).run()
```

默认插件由 composition 单点装配；启动、停止、恢复共用入口。插件是实现的组织方式，普通 Agent/Worker 配置没有 `process_owner`、`openclaw` 或 `plugin` 选择项。所需依赖在发行/安装阶段准备，运行任务时不临时下载；缺失或合同不兼容时在接纳业务前报错，不悄悄改用较弱的清理方式。现有 `jobs=` 只保留作高级可信注入的兼容入口，不要求普通调用者使用。

上游原模块、版本锁、必要桥接、平台差异和安装资产集中在默认插件中；核心执行逻辑不直接引用具体实现。是否还需要 Node 或完整 npm 包，由最小复用实现的验证决定，不由用户配置选型。将来独立发包时仍由框架发行/安装默认提供，不增加用户的 owner 配置。封装只隔离依赖，不能自动把约 103MB 的上游发布包缩小；最小原模块依赖提取、许可和构建仍须验证。

并发数量不决定任务是否持久化。有限 `map()` 仍是单次批次上限，durable 仍是跨 Run 的持久队列；统一的是执行尝试的资源生命周期。当前原型主要验证了 durable 路径，有限 map 中 live agent_factory、可信依赖、Run/trace 关联如何接入统一 Interface 仍需设计与验收，不能称为已实现。

## 版本与旧运行

- 为插件 Interface 定义版本，声明插件支持的框架范围；恢复描述标记 owner 身份、资源格式版本及必要上游版本，避免用当前配置猜旧调用归属。
- 执行器启动时装配默认插件；活动尝试不能热替换 owner。旧资源必须由能识别它的 Adapter 确认退出；缺失所需 Adapter 时拒绝启动替代尝试，不能丢弃资源描述。
- 初次交付保留 Application.durable、Worker 提交和现有调用方式、任务/队列身份、业务数据与可读旧回执。旧 launchd Adapter 的保留范围只用于兼容已存在资源及旧调用。
- 当前可增加接口并默认装配执行插件，以小版本增量交付。执行依赖及旧运行的迁移属于部署行为变化，需说明和验收；删除公开入口或改变既有数据合同才需要评估破坏性版本迁移。用户不需要通过新增 owner 配置才能启用新机制。

## 最小交付顺序

1. 从现有注入点提炼 owner Interface 和统一装配入口，集中授权取消及作用域合同；不新增通用插件平台。
2. 将原型实现与依赖集中到内置默认插件，让 NewsAgent 启动/停止/恢复通过框架入口；保留必要的兼容 Adapter，取消用户选择 owner 的配置。
3. 以同一公开 Interface 验证启动取消竞争、清理未确认、宿主强杀、旧资源恢复及容量不重叠；在 macOS/Linux 验证真实 Pi/MCP，最后跑已授权的 DeepSeek v4.1/AnySearch 完整日期。

上述是方案，不是实施报告。仍按用户要求暂停代码修改，原型的已有绿色测试不替代插件拆分后的验收。
