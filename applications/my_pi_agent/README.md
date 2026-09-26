# my_pi_agent

一个单 Agent Pi Application，用于阅读 AgentLoom 项目文档并回答 YAML 中配置的问题。

- 输入：Agent YAML 的 `task` 字段；长期行为写在 `system_prompt`。
- 输出：Agent 的文本回答。
- 分工：只有一个 Supervisor；使用 Pi 原生 `read` 读取相关文档，不调用 Worker，
  也不会修改文件。
- 模型：使用项目根目录 `config/llm.yaml` 中的 `codex_luna` 配置，调用 Pi 的
  `gpt-6-luna`，默认 `xhigh` 推理和 `auto` 原生搜索。先通过 Pi 登录一次。

另有 `workflows/my_pi_search_agent.yaml`，选择 `codex_luna_search`，要求每次
模型请求都完成 Pi 原生联网搜索，并把结构化来源显示为可点击链接。
- 本应用关闭 checkpoint 和 self-learning；相关配置位于 `config/system.yaml`。

其他环境首次运行前安装 Pi runtime（需要 Node.js 22.19+ 和 npm）：

```bash
.venv/bin/loom runtime install pi
```

运行：

```bash
.venv/bin/loom run applications/my_pi_agent/workflows/my_pi_agent_agent.yaml
```

定义校验：

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/my_pi_agent
```

本工作区的历史验证（2026-09-26，切换模型前）：定义校验通过；Pi 执行了
1 次 `read`，共 2 轮模型交互。2026-09-27 的订阅验收使用 Pi 0.87.1 和
`gpt-6-luna`：搜索工作流发出 1 次 `required` 请求，记录 4 次完成的原生搜索
调用和 5 条结构化引用，答案提供可点击来源。
