# web_search

此 Application 的两个 Agent 都从各自 YAML 的 `task` 读取用户任务，并通过 AnySearch MCP 获取实时搜索结果。长期规则写在 `system_prompt` 中。

运行前，将 `config/.mcp.anysearch_example.json` 复制为 `config/.mcp.anysearch.json`，并在本机填写可用的 AnySearch `Authorization` 值。实际配置文件不提交到仓库。缺少它时，定义校验会明确报 `MCP config file not found`，运行不会绕过搜索工具编造实时信息。

```bash
uv run python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/web_search
uv run loom run applications/web_search/workflows/web_search_agent.yaml
```

`us_after_close_a_share_signal_agent.yaml` 也是同一 MCP 配置的使用者；其报告生成任务独立定义在该 YAML 中。
