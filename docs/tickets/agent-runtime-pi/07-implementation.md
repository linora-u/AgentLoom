# 07：无工具 Pi Application 实现与交接

状态：完成。实现 revision：`a7907669f39408944b55afd47acf96a91964172a`。
基线：`9d5a88915efbd402d0f2697318125933fc1500e0`，包含已验收的 03。
分支：`codex/pi-t07-runtime`；交接引用：`refs/agentloom/ticket07-frozen`。

## 交付内容

- 通过现有 YAML、builtin registry、readiness、`execute_app` 和 CLI 运行真实 Pi AgentSession，返回原有 Application Run receipt。
- Pi 原生 Chat Completions / Responses 模型接口；解析现有 profile、有效 headers、生成预算、超时、重试及显式 provider 参数。
- 空工具 manifest、独立临时用户目录、内存会话；关闭基础工具、扩展、Skill、prompt、context 文件及用户设置发现。
- AgentLoom Hook Run 保留 Stop 决策。阻止后向同一 Pi 会话追加原因，上限到达则失败；最终完成事件在 Stop 放行之后发出。
- v1 双向 JSONL 关联、严格校验、模型阶段取消、进程关闭、协议故障与 CLI stdout 隔离。07 收到工具回调时明确拒绝。
- 独立 Node package、完整 lock 和 build/typecheck 入口。SDK 固定 `@earendil-works/pi-coding-agent@0.79.4`，要求 Node >=22.19；兼容选择避开其它工具预置的 Node 18。

详细配置映射与运行方式见 [Pi adapter README](../../../src/adapters/pi/README.md)。共享接线只修改 registry 与 runtime_options 校验；未改 Python lock、smol 实现、公共工具治理或长期记忆。

## 验收

| 07 验收要求 | 实际证据 |
| --- | --- |
| 真实 SDK / 原生 AgentSession | 生产 bridge 使用发布 SDK；测试只替换远端模型 HTTP 服务，不替换 runtime/registry |
| 原生模型映射及明确拒绝 | Chat/Responses 请求、参数/header、重试、流式超时、非法设置与缺 Node 用例 |
| 公共入口与 Run receipt | YAML `execute_app` 和真实 CLI；检查 manifest、运行事件、usage 与唯一 Run 身份 |
| 空 manifest / 无隐式发现 | 实际 HTTP 请求无 tools；项目 `.pi` 扩展、设置和 AGENTS.md 不进入 Pi |
| 不继承 smol 基础工具 | 历史全局 core toolset 不向 Pi 注入 Todo/final_answer；未选择的模型工具调用立即失败 |
| 取消、退出、协议故障、stdout | 实际子进程 SIGINT/SIGKILL、坏 JSON、错 Run/sequence、重复 key/回调/终态及 close ack 后重复终态 |
| Stop 与能力边界 | Stop 放行/继续/持续阻止；Goal、checkpoint 与未知 options 拒绝；不声明 Worker/工具/恢复 |
| 可复现 Node 产物 | `npm ci --ignore-scripts`、`npm run build`、TypeScript 检查；锁文件由 Pi 链维护 |

完整回归：**4256 passed、1 skipped、3 warnings**。此轮从 `d79b674e` 开始；之后仅有关闭缓冲帧顺序和有限数值校验加固，最终 `a7907669` 的协议回归 **7 passed**。新增/接线的五个 Python 模块类型检查无错误。第三方 warnings 与原基线相同。

真实模型验收：最终实现使用私有 `llm.yaml` 原配置，运行 **32 个独立 Application，32 个唯一 Run receipt，全部通过**。覆盖 4 个 Chat、4 个 Responses profile，各执行固定输出、计算、中文、JSON 四个场景；没有改写模型 profile 参数。测试框架另有确定性 HTTP 故障用例，未把这些用例算作真实模型调用。

首次全量组合暴露其它工具将 Node 18 放到 PATH 前面；已修复为实例内选择兼容 Node，不改全局 PATH。Standards 发现重复回调 ID 未去重；Spec 发现晚到的重复终态可能被 close 吞掉；均有失败回归并已修复，最终两轴复审均为零遗留 finding。

命令、报告路径、摘要和提交历史见 [07-validation.json](07-validation.json)。私有配置复制权限为 0600，受 Git ignore 保护，未进入提交或公开报告。

## 增量提交

1. `6e79e186`：锁定生产 SDK，加入失败的真实 Application 入口用例。
2. `d47d0de5`：接通原生会话、模型、Stop 与公共入口。
3. `f38470ec`：补取消、异常、超时、无工具终态与真实应用验收。
4. `d79b674e`：修复审查发现的协议重复消息与 Node 版本选择。
5. `a7907669`：保留关闭过程中晚到的协议失败。

## 09 / 13 接手

09 仍须同时具备 **05、07、08** 的已集成、已验收版本；07 完成不单独放行 09。消费 `PiRuntime`、v1 transport 和 bridge，接入 05 的 native host 及 08 的平台工具。工具引入后必须重新验证重试不会重放副作用；不能直接扩大 07 的无工具重试循环。Pi 内存会话续跑不等于 checkpoint/resume。

13 消费 `src/adapters/pi/bridge/` 的 manifest/lock/build，以及相邻 `bridge-v1.schema.json`；不重写 Node lock。发行安装仍由 13 交付。07 不提前实现工具、Worker、Goal 或恢复。

交付沿用 main 未提交、未暂存 Changes 的约定；增量历史保留在分支/冻结引用。只清理本票创建的 t07 worktree，其它并行 worktree 与既有 main 改动保留。
