# 架构重构：最终验证与交付范围

实现与验证已完成。维护者明确要求将最后发现的 **Skill 嵌套 workflow 扫描问题**
独立记录为 [#69](https://github.com/linora-u/AgentLoom/issues/69)，后续修复，
先提交本次已完成内容。本次没有合入该问题的修复，也不把它记录为已解决。

完整的脱敏结果、Run/task 身份、定义摘要、计数与证据哈希见
[机器验证记录](architecture-final-validation.json)。

## 实现

源码直接位于 `src/application/`、`src/runtime/`、`src/adapters/` 等职责模块下。
标准安装配置将 `src/` 映射为唯一的 Python 包 `agentloom`；例如
`agentloom.application.runner` 对应 `src/application/runner.py`。
旧 `src.*` 导入、旧模块命令和兼容转发已移除。

Application 的 YAML/Markdown 读取、拓扑、配置合成与来源、路径、调用快照由共享
实现负责，Studio、运行入口和 Skill 校验复用这些规则。AgentLoom 编排与状态归入
runtime；依赖上游 smolagents 对象的代码归入 adapter。文档、Skill、示例、模板和
仓库内调用方同步迁移。历史 checkpoint 仍作为持久化数据恢复，不依赖旧导入别名。

冻结实现：`cb13d6cdfae16385b9feca857f4e8f75343ae62d`。
最终源码树：`03f18452943e7518dbd11de41ded5a2b9026070b`。
后续交付文档提交不改变测试过的实现。

## 确定性检查

| 检查 | 结果 |
|---|---|
| Python 全量 CI 超集，含两份 memory campaign 合同文件 | **4060 passed，2 skipped，0 failures/errors**；247.93 秒 |
| 测试收集 | 4062 项，基线 3769 项，增加 293 项；基线测试组计数无减少 |
| 额外 Repo Map / 离线 memory Application 测试 | 87 passed；相关内容未变 |
| TUI | **200 passed**，752 assertions；typecheck、build 通过 |
| editable / wheel 隔离安装 | 各 **11 项通过**：外部目录导入、资源、CLI、真实 runner、生成入口、自定义工具、Run 与 Studio |
| clean archive wheel | 357 份源码/资源逐字节匹配最终实现，只包含 `agentloom` 包和分发元数据 |

两个既有跳过分别为当前机器无 Docker CLI 的真实 Docker 测试、Windows 专用
wexpect 测试。Python 全量日志保留两条依赖弃用警告，以及一条后台监控线程向已关闭
测试捕获流写日志的 `Bad file descriptor` 警告；未修改运行时日志代码来压制它，
也不将其声称为已修复。安装探针使用确定性模型，和下面的真实模型验收分开统计。

## 真实 Application 验收

实际响应模型为 `deepseek-v4-pro-260425`。每次执行均保留有界运行时间、真实
Worker/Tool 事件、Run/task receipt、定义摘要、产物和独立断言。

| 场景 | 最终结果 |
|---|---|
| F1 Native + F2 CodeAct | 两种模式各同进程连续两次；两种嵌套版本各一次，共六次复杂 Run 通过最终独立复核 |
| 新复杂 Application 独立检查 | 宿主 pytest 204 项；oracle **300/300**；额外零价探针 36/36；原始缺陷负对照产生 129 个预期失败；24 个 Worker 调用、18 条完整传递链 |
| F3 Unit Test Studio | 五 Worker 链路；宿主再次执行生成测试，11 passed |
| F4 Repo Map | 三目录 Worker；符号、跨目录引用、排名、依赖和 Skill 产物均验证 |
| F5 ContextEngine | text、JSON、multi-worker 三类；真实 ContextRef 来源与检索关联通过 |
| F6 checkpoint | 当前和旧版本状态各三类，共六场；同 task、新 Run，输入 hash 保持，五类副作用各一次；历史检索/回滚和已完成 Worker 输出/用量保留 |
| F7 Goal | bounded 正常完成；parallel 达预算后恢复完成，累计用量保留、已完成批次不重跑 |
| F8 工具 Application | Core 与 Markdown 均真实执行，独立核对调用和产物 |
| F9 拒绝/阻断 | 三类静态错误在分配 Run 前拒绝；真实策略阻断产生持久化 blocked 记录，无被禁止的写入 |

真实模型 Run 按各自实际执行版本记录，主要源码树为 `3950f642…`，**没有改写成
在最终源码树上执行**。后续四个源码文件变化仅涉及已废字段的提前拒绝、Studio
Markdown 定义发现/入口一致性。两个版本对 **23 份实际请求、63 个定义节点**的
解析、有效配置、模型目录、来源、执行配置和公开投影哈希完全一致；该对比没有
执行模型、工具、Hook 或分配 Run。新增非法定义和 Markdown 入口有专门回归及
真实 CLI 检查。Spec 审阅独立重算了这些结果并确认影响范围。

## 失败记录与修复

- 复杂 Application 曾遗漏非空零价购物车行为、误把报告对象当路径。完善了合同、
  Worker 指令与报告写入校验；保留原独立 oracle、缺陷 fixture 和全部失败记录，
  修复后重新执行完整六场。
- 验收器曾误选同角色第一次调用，并过度要求 query 的外层文本也是合法 JSON。
  现在按 task/Run/call、输入 hash、时序和真实工具事件关联完整结果；JSON 值的
  字段、类型、列表顺序仍严格匹配。Native 第二次原始误报保留，独立复核从
  字符 12489–13534 重放了完整的 1045 字符结果；未修补原始输入或重跑模型挑结果。
- checkpoint 早期中断点、继承的 30 秒代码块预算、恢复时输入换行变化分别导致
  验收失败。修正已提交进度门槛，示例声明有限的 1200 秒预算，并使用唯一固定
  query literal；随后重建旧版本状态并重新执行全部六场。上游超时算法和 Worker
  输入 hash 规则保持不变。
- Skill 的独立解析/配置规则曾误报有效相对路径与 MCP null，已改用共享校验。
  接入时发现 `tools_mapping` 预检与实际 Agent 拒绝不一致，已共享同一拒绝规则。
- Studio 漏发现 Markdown Supervisor，已补齐发现、详情、Run 引用和 schedule
  目标格式检查，并保留路径、symlink、Worker 与副作用边界。
- 旧 worktree 的残留 `build/lib/src` 曾污染 wheel。该产物保留为失败证据；本次
  最终 wheel 从 clean git archive 构建，并逐文件对照 Git 内容。

## 审阅与已知后续项

Spec 轴无未解决 finding。Standards 轴发现一个 P2：两个 Skill 脚本漏扫嵌套
workflow 目录，可能漏报非法嵌套定义。按维护者明确决定延期到 [#69](https://github.com/linora-u/AgentLoom/issues/69)。
涉及嵌套 workflow 时，应使用共享的领域 `application.validate` 检查，不能以当前
两个 Skill 脚本的成功结果证明完整 Application 已通过校验。

## 复跑与提交

使用 `uv sync --python 3.12 --locked --all-groups` 安装项目。完整 Python 命令为：

```sh
uv run pytest tests/ \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py
```

真实复杂任务入口见 [Application README](../../applications/architecture_contract_validation/README.md)，
其余功能入口与运行限制见框架 Skill 的 validation reference。
本地私有模型配置、原始请求、全部失败日志和冻结 checkpoint 保存在仓库外的
`agentloom-architecture-notes`，不提交到 Git。

提交通过 [PR #68](https://github.com/linora-u/AgentLoom/pull/68) 进入受保护的
GitHub main；本地 main 通过 fast-forward 集成本次结果。独立配置讨论 #17/#19
和 MCP lifecycle 规格不包含在本次交付中。
