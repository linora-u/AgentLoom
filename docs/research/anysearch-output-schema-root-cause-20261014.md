# AnySearch「7 个凭据耗尽」最小诊断

日期：2026-10-14。范围：全日 v2 的首个 validation 失败，只读诊断，不修改业务、SDK 或 campaign，不调用模型。

## 已确认的结论

对原失败 URL 使用**第一个凭据、官方 MCP ClientSession、一次 extract 调用**，重现的是输出协议错误：

> `RuntimeError: Tool extract has an output schema but did not return structured content`

连接、工具枚举和 tools/call 的 HTTP 状态均为 200；初始化通知为 202。原输入通过服务端当次返回的 inputSchema 验证。这个复现证明，至少一种能够触发当前失败路径的原因是 MCP 返回不符合已声明的输出合同，不能把应用最终的「凭据耗尽」解释为七个账号都没有额度。

没有请求第二至第七个凭据。首次诊断仅记录 HTTP 状态，未保存 tools/call 原正文；原全日日志同样没有保留逐凭据原因。因此结论是已经复现的输出协议错误被错误地当作换凭据原因，不追认原七次远端正文，不把此次失败归为额度问题。按主任务指示，不再发起额外请求。

## 原 trace 与最小复现

原任务：`validation-6bdb0fe953cc4f468b0a890ffcbc46c4`。Run：`run_20261013T211039060445Z_4766c9c2514f`。

第 8 步并行发出两个 extract：Bing 调用存在 completed 工具事件；DuckDuckGo 调用只有 PreToolUse，随后 Agent 结束为 failed。失败调用为：

```json
{
  "name": "mcp__AnySearch__extract",
  "arguments": {
    "url": "https://html.duckduckgo.com/html/?q=%E9%9B%B6%E8%B7%91%E6%B1%BD%E8%BD%A6+8%E6%9C%88%E4%BA%A4%E4%BB%9857066%E5%8F%B0"
  }
}
```

通过公开 RunTrace API 读取并验证 model response 与 PreToolUse payload，二者输入一致。此前该 Worker 的 get_sub_domains、batch_search、search 及一次 extract 已完成。

| 诊断项 | 第一个凭据的观察 |
|---|---|
| initialize | HTTP 200 |
| initialized notification | HTTP 202 |
| tools/list | HTTP 200 |
| extract tools/call | HTTP 200 |
| 原输入与 live inputSchema | 验证通过 |
| 官方 SDK 结果 | 输出 schema 缺少 structured content，RuntimeError |
| 关闭后资源 | 剩余 async task 0，新存活线程 0，portal false |

MCP SDK 为 1.26.0，httpx 为 0.28.1，AnyIO 为 4.11.0。

## 因果链与修复边界

1. 官方 ClientSession.list_tools 缓存服务端的 outputSchema。
2. ClientSession.call_tool 在 `not result.isError` 时校验结果；这次校验进入 `_validate_tool_result`，说明服务端没有标记 MCP isError。
3. 声明了 outputSchema 但 structuredContent 为 None，官方 SDK 必须抛出上述 RuntimeError。
4. 全日 v2 故障时的 AnySearchSession._call 对任意 Exception 调用 `_advance()`，因此协议错误也被当作换凭据的原因；最后的错误丢失实际原因，只报告凭据耗尽。

最短的修复路径是保持官方 SDK 校验，按原因区分可切换凭据的认证/服务错误和应当保留为模型工具错误的本地/协议失败。不能通过删除输出校验、修改 SDK 私有缓存或放宽通用 MCP cleanup 来让验收通过，也不能只靠「耗尽」猜测额度。

此次诊断没有修改产品。主任务随后实施了明确允许的 HTTP/服务错误分类；本子任务只读核对如下。

## 修复的 Type 边界复核

上游没有保证所有本地或协议错误都使用 plain RuntimeError。最终 `_can_failover` 因此采用允许列表，两个入口 `get_tools()` 与 `_call()` 都遵守同一判断：

| 错误类型 | 是否换凭据 |
|---|---|
| 已知 SDK 输出合同错误、普通 RuntimeError | 否 |
| 输入 ValidationError、发现阶段 SchemaError、缺少四个官方工具 | 否，脱敏报错；不尝试备用凭据 |
| 未知 SDK/MCP/Pydantic 格式错误 | 否 |
| AnySearchServiceError、McpToolExecutionError、TimeoutError | 保留原 failover |
| httpx.HTTPError | 保留原 failover |
| SDK McpError，code 为 HTTP 408 | 保留原 failover |
| 异常组 | 仅每个叶子均符合允许列表时 failover |

安装版 `mcp.shared.session.py:294–300` 确实使用 `httpx.codes.REQUEST_TIMEOUT` 构造 response timeout，不是 TypeScript SDK 的 `-32001`。真实适配器在发现时的 `check_schema` 可抛 SchemaError，调用前的 `validator.validate(arguments)` 可抛 ValidationError；两者都不在允许列表。缺少官方工具的普通 RuntimeError 也不在允许列表。以上是源码边界核对，没有用本地失败去请求实际备用 key。

清理使用更严格的独立边界：`_advance` 只接受纯 httpx.HTTPError 异常组，任何未知/非 HTTP cleanup 叶子继续抛出。通用 MCP cleanup 没有放宽；SDK 的 input/output 校验也没有被绕过。

`McpToolExecutionError` 仍表示通用的显式 MCP isError，`AnySearchServiceError` 仍表示既有顶层 failure 字段，未进一步区分额度、权限和其他服务错误。这保留用户已授权的原 failover 规则，不证明每一种服务失败都能靠换 key 恢复；不根据中文错误消息猜额度，也不假设 REST 的 error_code 会出现在 MCP。此次没有追加任何实际 API 请求。

## 一手来源与证据

- 官方 Python SDK [ClientSession，v1.26.0](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/src/mcp/client/session.py)：call_tool、_validate_tool_result、list_tools。安装版实际函数源码副本保留在证据目录。
- 官方 AnySearch [MCP README，固定 e3f2701](https://github.com/anysearch-ai/anysearch-mcp-server/blob/e3f2701a21e61419ebd140358f0b1abb1f5ded04/README.md#extract)：extract 接受 HTTP(S) 页面 URL，提取 HTML 内容。原输入符合这个入口；页面抓取能否成功是另一层问题。
- 当前应用 `applications/news_agent/anysearch.py` 的 `_call` 与 `_advance`；源 SHA-256 在诊断时写入 sources 文件。
- [原调用与 live 单次诊断证据目录](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/.agentloom/validation/anysearch-root-cause-20261014-v1)：`original-input.sanitized.json`、`ordinal-1.sanitized.json`、`probe.py`、上游函数源码、版本与 evidence-manifest。

所有正文在落盘前先显式替换全部七个 Authorization 及其 bare token，再经框架 redact_text。没有保存 credential、完整 header、环境变量或原私有配置。官方 async session、transport、HTTP client 已退出，诊断没有启动 blocking portal。
