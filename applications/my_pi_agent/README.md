# my_pi_agent

一个单 Agent Pi Application，用于阅读 AgentLoom 项目文档并回答通过 `--task`
传入的问题。

- 输入：`--task` 中的问题文本。
- 输出：Agent 的文本回答。
- 分工：只有一个 Supervisor；使用 Pi 原生 `read` 读取相关文档，不调用 Worker，
  也不会修改文件。
- 模型：使用项目根目录 `config/llm.yaml` 中的 `powerful` 配置。
- 本应用关闭 checkpoint 和 self-learning；相关配置位于 `config/system.yaml`。

其他环境首次运行前安装 Pi runtime（需要 Node.js 22.19+ 和 npm）：

```bash
.venv/bin/loom runtime install pi
```

运行：

```bash
.venv/bin/loom run applications/my_pi_agent/workflows/my_pi_agent_agent.yaml \
  --task "解释一下 AgentLoom 的 Application 是什么"
```

定义校验：

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/my_pi_agent
```

本工作区验证结果（2026-09-26）：定义校验通过。
用上述问题进行真实运行，Run 状态为 `completed`；Pi 执行了 1 次 `read`，
共 2 轮模型交互，回答引用了 `docs/cn/README.md`，未出现 DSML 伪工具调用。
Run 记录确认 checkpoint 为关闭状态。
