# AnySearch 工具错误与凭据切换合同

2026-10-14。**`CallToolResult.isError` 不能证明凭据失效或额度耗尽。** 本轮只读公开文档、固定源码与产品 adapter；未读凭据、请求 AnySearch API、运行模型或修改产品／campaign。[来源与 SHA-256](../../.agentloom/validation/anysearch-error-contract-research-20261014-v1/SOURCE-MANIFEST.json)

## 已核实的信号

MCP 的 tool execution error 包含 API、输入和业务错误；2025-11-25 规格明确要求把这类反馈提供给模型，使其修正参数后重试。AnySearch 安装页声明支持 2025-03-26，那个版本同样把输入／业务失败放在 `isError=true`，并非账号错误专用通道。[MCP 新版错误语义](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#error-handling)、[2025-03-26 规格](https://modelcontextprotocol.io/specification/2025-03-26/server/tools#error-handling)、[AnySearch MCP 安装](https://anysearch.com/docs/mcp-install)

| 信号 | 官方能支持的判断 |
| --- | --- |
| AnySearch REST `/v1/extract` HTTP 400／422 | URL 无效／无法提取目标内容，不能因此耗尽 key |
| REST HTTP 401／402 | Authorization 或 key 无效／额度耗尽 |
| REST HTTP 403 | 过期 key、禁用账号或 forbidden access；不足以细分这三者 |
| REST HTTP 429 | key、用户或匿名 IP 限流；不等于永久额度耗尽 |
| REST `code=-1` | 共用错误标记，不能区分账号和目标错误 |
| REST `error_code` | 可选；extract 文档明确示例为 `invalid_extract_url`。认证错误示例没有此字段；未找到完整账号错误码表 |
| REST `request_id`／`X-Request-ID` | 服务端生成的关联 ID，可用于诊断，不是错误分类 |
| MCP `isError`／`content` | 调用失败标志与原工具反馈，没有标准账号／额度含义 |

REST 状态与示例来自 [extract 官方文档](https://anysearch.com/docs/api-endpoints/v1-extract)；共用 envelope 和关联 ID 来自 [API 官方文档](https://anysearch.com/docs/api-endpoints)。这些 HTTP 状态必须是 AnySearch 自己的 REST 响应状态，不能拿 JSON-RPC code 或正文里的目标站状态代替。

## SDK 保留什么，当前 adapter 丢了什么

固定 MCP Python SDK 1.26.0 tag `3d9d34552a9ab8988acf8d06e4a705bd799fc32b` 的三份源码与本机安装包一致。`ClientSession.call_tool()` 对 `isError` 仅跳过成功结果 schema 校验，然后返回原 `CallToolResult`；该类型支持 `content`、可选 `structuredContent` 和 `_meta`，没有标准 HTTP status 字段。JSON-RPC 异常的 `McpError.error.code/data` 属于协议层，也没有自动映射为 AnySearch 账号错误的合同。[ClientSession](https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/src/mcp/client/session.py#L368-L397)、[CallToolResult](https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/src/mcp/types.py#L1363-L1369)

当前 [adapter](../../src/integrations/mcp/adapter.py) 先把 `isError` 变成只带 message 的 `McpToolExecutionError`，不保留完整 structured result／meta；[AnySearchSession](../../applications/news_agent/anysearch.py) 再对捕获的任何 Exception 执行 `_advance()`。因此，账号分类之前已经丢失了可能有用的原结构。

官方 AnySearch Skill 固定 `9b91ee215164de60d61c7d63789c891a1948b039` 的 Python／Node CLI 直接调用 REST，保留实际 HTTP status、request ID、data，但不保留顶层 `error_code`，也不实现账号／目标分类。这不是 MCP 服务端映射的证明。[Python 源码](https://github.com/anysearch-ai/anysearch-skill/blob/9b91ee215164de60d61c7d63789c891a1948b039/scripts/anysearch_cli.py#L68-L106)、[Node 源码](https://github.com/anysearch-ai/anysearch-skill/blob/9b91ee215164de60d61c7d63789c891a1948b039/scripts/anysearch_cli.js#L47-L89)

## 未知边界与最小修复

公开 AnySearch MCP 仓库固定 `e3f2701a21e61419ebd140358f0b1abb1f5ded04` 只有文档、许可证和 issue 模板，没有服务端 handler。未找到“REST HTTP status／error_code 必定进入 MCP structuredContent 或 `_meta`”的官方保证，不能虚构这些字段或借注册接口的错误码表分类 extract。[固定仓库](https://github.com/anysearch-ai/anysearch-mcp-server/tree/e3f2701a21e61419ebd140358f0b1abb1f5ded04)、[实际文件树](../../.agentloom/validation/anysearch-error-contract-research-20261014-v1/MCP-PUBLIC-TREE.txt)

最小修复应先保留原 MCP error 结构，让目标／未知工具失败按协议进入 Agent 的可纠正反馈，**不因 `isError`、`code=-1` 或泛 Exception 消耗下一个 key**。凭据切换只依赖已核实的账号层结构化信号；若实际 MCP 回执没有这些信号，就保留未知，不做 message 子串猜测。本报告不判断 DuckDuckGo 的具体失败原因，也不声称 7 个账号的真实余额或认证状态。
