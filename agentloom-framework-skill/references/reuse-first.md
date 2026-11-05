# AgentLoom 复用门禁

开发 Application 时，先使用框架已有的声明和生命周期接口。Application 只保留领域输入准备、领域 Tool、领域校验、确定性汇总和必要的薄入口。

## 开发前必须完成的映射

先列出需求与现有接口的对应关系。每个需求必须落到“复用接口”或“框架缺口”，不能直接落到新文件。

| 需求 | 优先使用的 AgentLoom 接口 |
|---|---|
| 定义 Agent、任务、Prompt、模型类型和工具 | `workflows/*.yaml` |
| 加载和校验 YAML | `agentloom.app.definition` 的共享定义路径；运行时直接使用 `Application` 或 `loom run` |
| 运行 Application | `Application.open()`、`run_app()` 或 `execute_app()` |
| 调用一次 Worker | `app.worker(path).invoke(WorkItem(...))` |
| 有限批处理和并发 | `app.worker(path).map(...)` |
| 把 Worker 暴露为 Agent Tool | `worker_agents` 或 `YamlAgentFactory.create_agent_as_tool()` |
| 注册确定性 Python Tool | Agent YAML 的 `tools: [{name, module, function}]` |
| 根据本次 Worker 输入生成精确 Tool Schema | Agent YAML 的 Tool factory，返回 `ToolBinding`；通过 `current_tool_factory_context()` 读取调用输入 |
| Tool 参数和嵌套 Schema | `ToolDefinition.parameters`、`ToolBinding.input_validator` |
| Tool 成功后结束当前 Agent | `ToolBinding.terminal_on_success` |
| 限制 Agent 最大执行步数 | Agent YAML 的 `runtime_options.max_steps`；由对应 Runtime 执行 |
| PI 上下文自动压缩 | PI 原生 `runtime_options.compaction`；遵循 SDK 默认设置 |
| 模型、协议、凭据、超时和重试 | 项目或 Application 的 `config/model.yaml`，Agent YAML 只写 `model_type` |
| 日志、checkpoint、toolsets、MCP、自学习 | `config/system.yaml` 或已允许的 Agent YAML 覆盖 |
| Hook 生命周期行为 | 显式 Hook Bundle |
| Skill 指令和资源 | Application 或项目 `skills/` |
| PI / smolagents 差异 | 对应 `src/runtimes/` Adapter；Application 不包装 Runtime |

完成标准：实现说明中能看到每个需求复用了哪个接口；标为框架缺口的事项有源码搜索结果、现有接口不能表达需求的具体原因，以及应新增的最小通用接口。

## Application 应保持的形状

推荐：

```text
applications/<app>/
├── workflows/*.yaml
├── agent_tools/          # 领域函数、Tool factory、Schema 与校验
├── config/               # 仅需要的 Application 覆盖
├── <app>_app.py          # 仅在需要预处理、批处理或确定性后处理时存在
└── README.md
```

薄入口只做以下工作：

1. 准备并校验本次业务输入。
2. `Application.open(...)`。
3. `app.worker(...).invoke(...)` 或 `.map(...)`。
4. 验证和返回业务产物。

领域 Tool 可以读本次显式绑定的业务依赖，执行领域校验并保存业务产物。模型调用、YAML 解析、Tool Gateway、执行预算、运行时重试和 Run 状态由框架管理。参数错误直接返回当前 Agent，是否继续由 Runtime 的预算和生命周期决定。

## 需要停下检查的信号

准备新增以下代码时，先搜索上述接口和相关测试：

- `YamlConfiguredAgent` 子类，尤其覆盖 `_build_tool_gateway()`、`build_runtime()` 或模型选择。
- Application 自己实现的 Runtime、Tool Gateway、Worker 调度器、错误协议、日志或 checkpoint。
- Tool 内硬编码 `max_turns`、失败修正次数，或读取 Hook 步号来控制 Agent 结束。优先用 `runtime_options.max_steps`，不把预算字段放进 `model.yaml`。
- 手动解析 Agent YAML、复制 Agent 配置白名单或自行推导 Application/Worker 路径。
- Application 从另一种配置文件加载模型，再拼装 `LLMConfig`，而标准 `config/model.yaml` 已能表达相同内容。
- 用 Prompt 约定代替 AgentLoom 已支持的 `input_schema`、`output_schema`、Tool Schema 或 `terminal_on_success`。
- 为一次调用再包一层只转发参数和返回值的 Adapter。

出现这些信号不代表一定禁止实现。先做删除测试：删掉新模块后，复杂度若不会回到多个调用点，它只是转发层，应直接使用框架接口；只有复杂度确实属于框架且会被多个 Application 复用时，才把能力加到 `src/`。

## 框架缺口的处理

确认缺口后，新增一个最小、通用、可测试的接口：

1. 接口放在职责所属的 `src/` 模块，不放进单个 Application 的自定义 Runtime。
2. Application 通过 YAML 或公开 Python 接口使用它。
3. 更新配置白名单、YAML 契约和用户文档。
4. 单测验证局部行为，至少一个真实 Application 验证完整链路。
5. 迁移当前 Application 到新接口，删除临时桥接和重复路径。

不能因为“先跑起来”长期保留第二套模型配置、第二套 YAML 解析、第二套 Tool 错误或第二套任务生命周期。
