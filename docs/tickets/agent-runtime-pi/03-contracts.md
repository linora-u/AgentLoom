# 03：运行合同与 Pi 桥接合同 v1

本票冻结可运行的兼容基线；生产 Pi 注册在 07，native 治理实现在 05/06。后续实现同时遵循 [工具归属](tool-ownership.md) 和 [文件交接](03-file-ownership.md)。本文件不扩大 01 已证明的恢复范围。

**状态：03 已验收。** 实现 revision 为 `9f08aa09ce3784d9c55eb82b01571256ded36ecf`；最终交接使用 `refs/agentloom/ticket03-frozen`，包含本目录完成记录。验证范围与保留限制见 [实现报告](03-implementation.md)。

## 1. 通用完整调用

公共入口仍是 `AgentRuntime.run(AgentRuntimeRequest)`、`snapshot()`、`close()`，类型位于 `src/runtime/agent_runtime.py`。Application 不消费 smol step 或 Pi message，也不要求未来 CLI/SDK 基座采用 Node/JSONL。

- `RuntimeDefinition` 给出实例、角色、指令、模型选择、已选工具、能力要求和后端 options；模型选择不要求 Python `ModelTurnBinding`。
- `RuntimeDefinition.tool_manifest` 是与已选工具逐项匹配的冻结快照，供 adapter 直接消费。公共 `tool_manifest_snapshot` 校验名称唯一、集合与 schema 一致；旧 ToolGateway 没有 metadata 扩展时，从 definitions 生成明确的 external/python 条目，不猜测提供方，也不丢失已选工具。
- `RuntimeModelSelection.settings` 是所选 profile 的私有快照；`request_headers` 是实例真正生效的 headers，优先级为全局 → Application → Agent → 模型 profile，header 名按大小写不敏感覆盖。解析实现共用 `configuration/model_request_headers.py`。
- header 合并在有效配置构造时逐层完成，同时覆盖直接 headers 和自定义 header profile。不能先按大小写敏感的字典深合并，再依靠最终字典顺序判断来源优先级。
- settings、headers、固定参数和原始工具参数可能含私密数据。它们只进入授权的实例传输/执行，不进入公开 metadata、repr、日志或指纹。协议解析错误不回显输入。
- `runtime_options` 继续使用 02 的来源归一化与 smol 旧字段兼容；其他基座不必理解 smol 的规划、Todo 或摘要参数。
- 结果状态仍为 success / max_steps_error / interrupted / failed；错误分类、usage、checkpoint envelope 继续使用现有合同。工具终态仍为 completed / error / blocked。
- Application Run 与 Hook local Run 不混为一谈：公共请求的 `run_id` 为 Application Run，Hook local Run 由 host 的实例调用上下文持有；adapter 不能自行选择其他 Worker 的 Hook Run。

## 2. 工具登记、选择和 manifest

三个独立登记入口都是纯 metadata：

| 登记入口 | 后续所有者 | 内容 |
| --- | --- | --- |
| `src/adapters/smolagents/tool_catalog.py` | 04 | 11 个自研基础工具，含 Todo |
| `src/tools/platform_catalog.py` | 08 | ContextRef、Skill 激活、记忆/历史和 Skill 管理 |
| `src/tools/optional_catalog.py` | 08 | 10 个专业工具，含 AST/LSP、大纲和 Markdown |

`src/tools/catalog.py` 聚合登记，不加载执行实现；`loader.py` 仅加载选中的实现，并把其不可变归属信息带到 binding。Goal、Worker 是应用装配时产生的平台工具；final_answer 是 smol 注入的基础工具。动态外部 Python 工具未声明归属时标记为 external/python，不按同名 builtin 猜测提供方。

各登记分区显式声明 operation 与 command_parameter。公共 Gateway 只投影 metadata，不通过 shell_tool 等基座工具名推导执行类别；Todo 和后台任务控制也不冒充文件写入。

`ToolManifestEntry` 位于 `src/runtime/native_tools.py`。字段包括 logical_name、visible_name、owner、provider、capability、operation、parameters、path_parameters、command_parameter 和私有 fixed_arguments。`AgentLoomToolGateway.manifest` 只包含实际选择的工具；smol 的包装 Gateway 补上自己的 Todo/terminal metadata。模型看到的 schema 仍来自原 `ToolDefinition`。

03 中已有的兼容解释：

- 旧 smol 工具名、六组历史默认 toolset、固定参数和原启动方式保留；显式 builtin 可覆盖其 toolset 条目的固定参数。
- 历史全局 core_file/core_shell/core_search 仅供 smol 默认装配，不向其他 runtime 加载这些 Python 实现。平台默认仍根据原配置处理，不强制开启新功能。
- 显式 Agent/Application 工具选择有约束力；未有已验证映射的旧基础工具在 native 上报错。03 不登记任何 Pi 基础工具别名映射。
- toolsets: [] 关闭隐式 builtin 集合；tools: [] 只表示没有显式工具条目，不能单独解释为关闭继承的 toolset。两者均为空且没有 MCP/Worker/Goal 等额外入口的 native 应用保持空 manifest。重复显式名称、动态覆盖同名默认项、MCP/Worker/最终装配的同名冲突不能靠注册顺序吞掉。
- 可选专业工具不加入默认集合。MCP 仍走既有生命周期；其构造去 smol 化属于 08。

内部装配重复引用同一个工具对象时，保留旧的去重行为；不同对象抢占同一名称则拒绝。显式 YAML 重复声明仍报错。

固定参数属于有效执行输入的一部分。后续 native 映射必须同时验证 schema、固定参数与权限语义，不能只替换 read_file→read 这样的名称。

## 3. native prepare / settle

通用合同在 `src/runtime/native_tools.py`；`NativeToolHost` 是 05 的实现接口，03 没有实现生产授权服务或持久 journal。

1. `NativePrepareRequest` 保留原始参数、cwd、已解析 manifest 与 call identity。
2. host 依次执行已配置的 PreToolUse 变换、严格 schema 校验、CoreToolGuard/授权和必要写前保护。任何失败都不给执行授权。
3. `NativePreparation` 要么返回 `NativeAuthorization`，要么返回 blocked/error `ToolCallRecord`，两者不能同时存在。
4. 授权绑定 Application、task、Run、instance、call、提供方、完整 manifest、cwd 与最终参数；原生 session/parent 锚点在可提供时一起绑定。`require_match` 拒绝任一错配。JSON 参数快照不会因为调用方随后改了原 dict 而改变。
5. **授权值本身不是一次性存储。** 05 必须持久保存消费状态，在授权发往执行器之前记录 executing，独立执行门以 host 保存的批准为权威，校验 authorization_id 和完整最终参数，再消费一次批准。不能拿收到的参数自我校验，也不能依赖 Pi 吞异常的 observer 去阻止执行。
6. `NativeExecutionOutcome` 的 completed/error 经过 host 原文产物与证据提交后，才返回带 commit_id 的 `NativeCommitAck`。观察者日志不是持久提交屏障。
7. 执行结果无法确定时，返回 journal 的 uncertain 状态，没有成功输出或伪造 ToolCallRecord。`NativeJournalEntry` 同时保存原始 request、最终授权以及可选的已提交结果。

| journal 状态 | 恢复语义 |
| --- | --- |
| prepared / authorized | 执行门尚未派发；只有持久状态能够证明未执行时，才按后续实现继续准备 |
| cancelled | 派发前取消，没有执行结果 |
| executing / uncertain | 不自动重新执行；保留不确定状态与已有证据 |
| committed | 可以按匹配的 native session/parent 与版本恢复结果，不能重复工具副作用 |

`ToolCallRecord` 仍只有三个既有终态，journal 状态不混入其中。原生 session、消息和 checkpoint payload 由基座解释，长期记忆和可信证据仍归平台。

共享可执行示例在 `tests/lib_test/runtime/native_contract_fixture.py`，场景在 `test_native_tool_contract.py`：参数修正、拒绝无副作用、备份先于执行、fsync 后确认、两个崩溃窗口、派发前/后取消。这里的微型 host 明确是 fixture；05/06 必须把场景接到生产 Gateway，09/10 再接真实 Pi。01 的真实 SDK 恢复证明仍由迁移后的独立程序保留。

## 4. Pi 专属 JSONL v1

值类型与编解码在 `src/adapters/pi/protocol.py`；跨语言 schema 是同目录 `bridge-v1.schema.json`。本模块可以导入，但 builtin registry 尚未注册 pi。

一行一个 JSON 对象。所有消息包含 version=1、kind 和 instance_id；Run 相关消息带 run_id。request/response 共享 request_id；event 关联发起 run 的 request_id 并携带从 1 开始的递增 sequence。双方请求分别使用 `host:`、`pi:` 前缀，避免双向回调 ID 碰撞。

| 方法/消息 | 方向 | 结果与责任 |
| --- | --- | --- |
| handshake | host → Pi | 返回实际 SDK/Node 版本、native contract 版本和实测能力；不匹配则停止构造 |
| run | host → Pi | 含任务、模型私有投影、指令、精确工具集合、options 与可选 checkpoint；返回一次完整调用结果 |
| snapshot | host → Pi | 返回 native checkpoint envelope 或显式 null；仅在支持的安全点取快照 |
| cancel | host → Pi | 指明被取消的 request_id；accepted 仅确认收到，终结仍由原请求响应表达 |
| close | host → Pi | 关闭该实例进程、工具和 pending 请求；重复调用幂等 |
| tool_prepare | Pi → host | 使用原始输入与 call identity 请求变换、授权、写前保护 |
| tool_settle | Pi → host | 提交实际执行结果，等待持久 commit ack；uncertain 不产生终态结果 |
| platform_invoke | Pi → host | 调用已选的平台/专业/MCP 工具；由 host 绑定正确 Hook Run 和 Worker 上下文 |
| event | Pi → host | run/model/tool/subagent/usage/checkpoint/terminal 等运行观察；不承担提交确认 |

07 的 transport 必须持续收取双向请求：host 等待 run 时仍处理 tool/platform 回调；09 验证实际回调继续与并发关联。不得用阻塞读取一个响应的方式造成相互等待。

响应只能包含 payload 或 error。run 与工具回调的 instance/Run 必须匹配；pending 表还须核对方向、method、request_id、一次响应与 event sequence。错配、重复终结、未知方法、非法 JSON、重复键、隐式类型转换和版本不符都不能被当作有效执行证据。stdout 专用于协议，诊断使用 stderr 且不得打印私密载荷。

EOF、进程死亡或协议故障必须结清 pending 请求；不能虚构成功。执行门已派发的调用进入不确定处理。取消先阻止未派发工作，再取消模型、平台回调和受管进程；关闭边界及超时实现归 07/09/10，恢复对账归 12。能力不足时明确拒绝，不回退 smol。

## 5. 公共资源关闭

`src/runtime/resources.py` 提供 register_resource、close_instance_resources 和 close_run_resources。键由完整 Application runtime_key、实例 ID 和资源 ID 组成；callback 捕获创建时的具体句柄，不能在关闭时重新查询“当前 Worker”来猜归属。

- smol Shell session 和后台任务已登记实际资源；关闭一个实例不会终止同 Run 其他实例的任务。
- `RuntimeInvocation` 在运行时关闭后清理该实例；即使 runtime.close 失败也继续清理并报告错误。
- Application 最终化调用公共 Run 关闭入口，不再导入 Shell 注册表。注册函数在没有绑定 Run 时不接管资源，调用方仍负责其生命周期。
- `src/runtime/subprocess_env.py` 是 Hook/执行器共用的环境规则。旧 Shell 路径暂为兼容导出，不再拥有实现。

04 按此合同迁移具体句柄、会话和资源状态；07 的 Node 进程也必须服从同一实例关闭边界。

## 6. 注册与版本变更

07 必须通过现有 builtin registry、Application 校验/readiness、`execute_app` 接通真实无工具 Pi。公开能力初始只涵盖已验证的无工具运行与 Stop/取消；09、10、12 分别扩展已验证的工具/Goal、写入/Shell、恢复支持。不能使用测试替身 registry 宣称生产 Pi 已接通。

通用 runtime API、native 工具合同、Pi JSONL 是三个不同边界。破坏性变更必须由协调者明确升级相应版本、同步 schema/fixture 与消费者，再让受影响 worktree 更新基线；不能在某一路私加临时字段。消息内容、Todo、原生压缩算法不会成为公共 runtime 兼容要求。
