# AgentLoom TUI 运行详情与 Skills 可发现性参考

研究日期：2026-07-18（Asia/Shanghai）

## 结论

AgentLoom 当前最需要解决的不是“少展示几行日志”，而是把内部运行数据转换成用户能做决策的信息。推荐采用四层渐进披露：

1. **项目总览**只回答“有什么、现在是否健康”：定义数量、明确分类的运行状态、最近异常；数量必须可以进入明细，不能成为无法解释的孤立数字。
2. **运行列表**每条只显示状态、Agent、时间/耗时和一行失败原因；不展示 run ID 全文、原始 events 或日志正文。
3. **运行详情**先显示结构化根因、失败位置、建议动作和日志路径；Agent 状态与语义时间线按需展开。
4. **完整日志/原始事件**属于调试证据，放到独立可滚动视图或磁盘文件；默认界面不渲染。失败详情提供“让 AI 分析”入口，由 TUI Agent 读取最小必要证据并给出根因分析。

这是 OpenCode、Codex CLI、Gemini CLI 和 Claude Code 的共同方向：主界面保留语义化摘要，把高噪声输出移到详情、transcript、debug console 或日志文件。OpenCode 的会话 UI 只把消息映射成 text/tool/reasoning 三类语义部件，不直接渲染底层事件流；通用工具输出默认关闭，即使打开也先压到 3 行，失败工具的完整错误需要点击展开（[会话部件映射](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1478-L1494)、[通用工具输出与折叠](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1791-L1825)、[失败工具展开](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1842-L1900)）。Codex CLI 把普通工具输出限制为 5 行并给出 `ctrl+t` 查看完整 transcript 的提示（[输出上限与完整 transcript 提示](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/exec_cell/render.rs#L33-L40)、[省略提示](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/exec_cell/render.rs#L253-L260)、[主视图 5 行布局](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/exec_cell/render.rs#L442-L474)）。Gemini CLI 则默认启用 compact tool output 和低错误冗余度（[官方配置](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/docs/reference/configuration.md#L328-L337)、[错误冗余度](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/docs/reference/configuration.md#L462-L467)）。

## 对当前两个问题的直接回答

### “4 Skills”为什么有问题

`4 Skills` 只是索引器的统计结果，不是完整功能。用户无法从这个数字知道：

- 具体是哪 4 个；
- 来自项目、用户目录、内置能力还是扩展；
- 实际扫描了哪些目录；
- 哪个 `SKILL.md` 最终生效；
- 是否启用，以及重名时哪个版本覆盖了哪个版本。

成熟工具都把数量连接到可发现入口。OpenCode 有可搜索的 Skills 选择器，至少展示名称和描述；后端保留每个 Skill 的 `location`，并扫描用户级 `.claude` / `.agents`、项目向上目录、OpenCode 配置目录、自定义路径和 URL 来源（[Skills 选择器](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/component/dialog-skill.tsx#L13-L68)、[发现来源](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/opencode/src/skill/index.ts#L173-L232)、[名称、描述和 location 投影](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/opencode/src/skill/index.ts#L321-L345)）。Gemini CLI 的 `skills list` 更直接：逐项打印启用状态、描述和实际 Location；其发现顺序明确区分内置、扩展、用户和工作区（[Skills 列表输出](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/commands/skills/list.ts#L27-L62)、[发现层级源码](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/core/src/skills/skillManager.ts#L50-L99)）。Claude Code 的 `/skills` 支持过滤、按 token 数排序和控制可见性，并公开了 personal/project/plugin 等实际路径规则（[官方命令说明](https://code.claude.com/docs/en/commands#all-commands)、[Skill 路径与优先级](https://code.claude.com/docs/en/slash-commands#where-skills-live)）。

因此 AgentLoom 的 `Skills` 计数必须可点击，进入列表后至少显示：

```text
Skills (4)                         /skills
Project 2 · User 1 · Built-in 1

✓ agent-builder          Project
  创建和校验 Agent YAML
  applications/.../skills/agent-builder/SKILL.md

✓ incident-diagnosis     User
  分析失败运行和日志
  ~/.agentloom/skills/incident-diagnosis/SKILL.md
```

列表应支持搜索；Enter 查看说明，`o` 打开源文件，`r` 重新索引。底部再显示“扫描来源”，包括每个实际根目录及其发现数量。若数量不可进入这个页面，项目总览就不应显示该数量。

### “6 failed/crashed + 全量日志/events”为什么有问题

`failed` 和 `crashed` 是不同故障域，不能合并：

- `failed`：运行器仍然可控，但某个 Agent、模型或工具返回终态失败；
- `crashed`：进程异常退出、信号终止或状态丢失，没有正常失败终态；
- `cancelled`、`unknown` 也不能被倒推出 `succeeded`。

因此不能用 `total - failed - running = succeeded`。总览只能统计存储中明确存在的终态；未分类记录单独显示为 `unknown`。否则“9 Runs、6 failed/crashed、0 running”并不能证明另外 3 次成功。

日志和 events 也不是同一种用户信息。日志是诊断证据；event 是内部状态传递记录。用户通常需要的是“哪一步失败、主要原因是什么、下一步做什么”，而不是 transport event 名称和整段 stdout/stderr。OpenCode 把 session message 的错误转成一块简短错误信息，而工具错误默认折叠（[会话错误摘要](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1519-L1533)、[工具错误按需展开](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1978-L1982)）。Codex CLI 的普通错误就是一行红色摘要，详细命令输出则进入 transcript（[错误 cell](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/history_cell/notices.rs#L213-L218)）。Gemini CLI 的 compact 工具错误会把换行压成单行并截断显示，过长输出直接保存到文件并只告诉用户路径（[错误摘要](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/components/messages/DenseToolMessage.tsx#L343-L365)、[长输出文件提示](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/components/messages/ToolGroupMessage.tsx#L440-L473)）。

## 上游模式对比

| 项目 | 默认运行信息 | 进一步展开 | 完整日志/调试 | Skills/commands 可发现性 | AI 诊断入口 |
|---|---|---|---|---|---|
| OpenCode | 语义化 message parts；工具有专用摘要；generic output 默认隐藏 | `/details` 切换工具详情；generic output 最多 3 行后点击展开；失败工具点击展开错误 | 内部事件用于同步 session，不作为默认列表 | `Ctrl+P` 命令面板按名称、描述、分类和快捷键展示；Skills 有搜索选择器（[命令面板源码](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/component/command-palette.tsx#L26-L78)） | 没有找到面向历史 Run 的专用入口；可以通过会话继续询问 |
| Codex CLI | `Ran <command>`、状态色和有限输出；探索型只显示 Read/List/Search 摘要 | 默认工具输出最多 5 行，保留头尾；`Ctrl+T` 打开完整 transcript | TUI 默认写有界诊断存储；显式配置 `log_dir` 才产生 plaintext `codex-tui.log`（[官方仓库说明](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/docs/install.md#L52-L63)） | `/` 弹窗按高频顺序列出命令与描述；Skills 支持 fuzzy search 和 enable/disable（[命令排序与描述](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/slash_command.rs#L7-L15)、[命令描述](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/slash_command.rs#L81-L103)、[Skill 搜索数据](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/bottom_pane/skill_popup.rs#L20-L58)） | `/review` 面向代码变更，不是运行故障诊断（[命令定义](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/slash_command.rs#L81-L103)） |
| Gemini CLI | compact tool output 默认开启；recoverable error 默认低冗余 | 可展开 tool 子视图；完整输出在独立可滚动区域 | F12 打开独立 Debug Console，非 debug 模式过滤 debug 消息（[Debug Console](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/components/DetailedMessagesDisplay.tsx#L27-L40)、[F12 视图](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/components/DetailedMessagesDisplay.tsx#L64-L85)） | `/skills list`、`/tools`、`/commands list`；命令列表展示来源目录（[Commands 列表源码](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/commands/commandsCommand.ts#L36-L75)） | 没有找到针对一次历史 Run 的专用 AI 诊断入口 |
| Claude Code | `/focus` 只显示最近 prompt、一行 tool-call 摘要和最终回答 | 切回普通视图查看细节；`/diff` 有独立交互查看器 | `--debug-file` 将 debug 日志写到指定文件（[CLI reference](https://code.claude.com/docs/en/cli-usage)） | 输入 `/` 查看全部命令并过滤；`/skills` 支持过滤和可见性管理（[Commands](https://code.claude.com/docs/en/commands)） | `/debug [description]` 开启调试日志，并由 AI 读取会话 debug log 来定位运行问题（[官方命令说明](https://code.claude.com/docs/en/commands#all-commands)） |

Claude Code 的两个设计最值得 AgentLoom 直接借鉴：`/focus` 证明“一行工具摘要 + 最终结果”可以作为正式视图，而不是信息缺失；`/debug` 则把日志视为 AI 的输入证据，而不是要求用户阅读的正文。官方说明中，`/focus` 只保留最近 prompt、一行工具调用摘要和最终回答；`/debug` 会开启本会话调试日志并读取日志排查问题（[Focus 与诊断命令](https://code.claude.com/docs/en/commands#all-commands)）。

## AgentLoom 推荐信息架构

### 1. 项目总览

```text
Workspace
AgentLoom

Definitions
25 Applications · 107 Agents
4 Skills                                      Enter 查看

Runs
0 Running · 3 Succeeded · 5 Failed · 1 Crashed
Scheduler stopped

Recent failures
× web_search_agent                    2m ago · 8.4s
  Provider authentication failed (401)        Enter 查看

× test_multi_workflow_agent          18m ago · 1.2s
  worker `extract` exited with code 1          Enter 查看
```

规则：

- `Definitions` 与 `Runs` 分组，避免让 107 个 Agent 看起来像正在运行。
- 只有异常状态使用高亮色；成功、总数和路径使用中性色。
- Recent 默认优先展示最近异常，而不是逐条重复 `run_2026...`。
- run ID 是技术标识，放进详情；列表只保留 Agent 名、相对时间、耗时和一行原因。
- `Failed`、`Crashed`、`Cancelled`、`Unknown` 分开；没有明确成功终态就不计入 `Succeeded`。
- `Skills` 必须是导航入口；详情解释每一项的来源与实际路径。

### 2. 运行详情

```text
Run failed
web_search_agent · 2m ago · 8.4s

Primary error
Provider authentication failed (HTTP 401)
at web_search_agent / search step

Suggested next step
检查当前模型配置的 API key 与 endpoint 权限。

[a] Ask AI to diagnose   [l] Open log   [c] Copy log path

Agent status (2)
✓ planner      completed  1.1s
× searcher     failed     7.2s

Log
~/.agentloom/runs/<run-id>/run.log

▸ Execution timeline (4 milestones)
▸ Technical details
```

默认区域必须稳定地回答六件事：

1. 运行的对象；
2. 明确终态；
3. 开始时间与耗时；
4. 哪个 Agent/步骤失败；
5. 一条经过清洗的主要错误；
6. 日志文件在哪里、下一步能做什么。

完整异常堆栈、模型响应体、stdout/stderr 和事件 JSON 不在默认区域。

### 3. 错误摘要生成规则

失败摘要不是简单取日志最后一行。应由运行器在失败发生时写入结构化字段：

```ts
type RunFailureSummary = {
  kind: "agent" | "model" | "tool" | "config" | "process" | "timeout" | "unknown"
  code?: string
  message: string
  agent?: string
  step?: string
  exitCode?: number
  occurredAt: string
  retryable?: boolean
  suggestedAction?: string
  logPath: string
}
```

展示优先级：结构化 `message` → 首个有意义异常 → 最后一个非包装异常。统一删除重复前缀、堆栈、序列化 JSON、密钥、Authorization header 和大段请求/响应体。UI 最多显示两行；技术错误码保留，因为它能改变下一步决策。

### 4. 日志

主界面只显示：

- 日志绝对路径或项目内相对路径；
- 文件大小与更新时间；
- 与主要错误相邻的 3–8 行清洗后预览（可选）；
- `Open log`、`Copy path`、`Tail log` 操作。

完整日志进入独立可滚动 pager，不能把运行详情撑成几百行。Codex CLI 的主视图/完整 transcript 分层和 Gemini CLI 的“输出过长时只提示保存路径”都支持这个边界（[Codex transcript 分层](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/exec_cell/render.rs#L248-L260)、[Gemini output file](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/components/messages/ToolGroupMessage.tsx#L454-L470)）。

### 5. Events

删除用户界面中的 `Events` 原始列表。若确实需要运行过程，则改名为 **Execution timeline**，默认折叠，只投影 3–10 个语义里程碑：

```text
19:01:08  Run started
19:01:09  planner completed
19:01:10  searcher called web_search
19:01:16  web_search failed · HTTP 401
19:01:16  Run failed
```

以下内容不进入 timeline：heartbeat、token delta、状态同步、重复 retry tick、原始 event type、完整 payload。原始 events 只保留在 `Technical details` 或 debug 导出中。这是从上游实现得到的产品推论：OpenCode 虽以事件同步 session，但最终 UI 只渲染 text/tool/reasoning 语义部件（[UI part mapping](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1463-L1494)、[PART_MAPPING](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/routes/session/index.tsx#L1564-L1568)）；Gemini CLI 也把 debug 消息放进单独的 F12 console（[Debug Console](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/packages/cli/src/ui/components/DetailedMessagesDisplay.tsx#L35-L40)）。

### 6. “让 AI 分析”

这是 AgentLoom 可以明显区别于普通日志查看器的入口。用户在失败详情按 `a` 后，TUI Agent 接收受限诊断包：

- `RunFailureSummary`；
- 失败 Agent 的 YAML 和父工作流中与它相邻的节点；
- 各 Agent 的最终状态；
- 主要错误前后少量日志和日志路径；
- 若是模型错误，加入已清洗的 provider/model/HTTP code/request ID；
- 不加入全量日志、密钥、环境变量值或无关 Agent 内容。

AI 输出固定为：**可能根因、直接证据、建议验证、建议修复**，并标明不确定性。它先只读分析；任何 YAML 修改仍然生成草稿，继续遵守 `/apply` 显式写入边界。

Claude Code 已把这个模式产品化：`/debug [description]` 会开启日志、读取 session debug log，并让模型针对用户描述诊断（[官方 `/debug` 说明](https://code.claude.com/docs/en/commands#all-commands)）。AgentLoom 不需要照搬整套命令；在 Run Detail 上提供上下文明确的 `Ask AI to diagnose` 会更短，因为 run ID、失败 Agent 和日志路径已经确定。

## 推荐键位与命令

| 位置 | 操作 | 结果 |
|---|---|---|
| Workspace | `Enter` on Skills | 打开 Skills 明细和来源 |
| Workspace | `Enter` on Run | 打开 Run Detail |
| Run Detail | `a` | 让 TUI Agent 分析本次失败 |
| Run Detail | `l` | 打开完整日志 pager |
| Run Detail | `c` | 复制日志路径 |
| Run Detail | `t` | 展开/折叠语义时间线 |
| Run Detail | `d` | 展开技术信息和原始事件 |
| 全局 | `/skills` | 搜索 Skills，并查看状态、scope、location |
| 全局 | `/runs` | 搜索和过滤运行记录 |
| 全局 | `/diagnose <run-id>` | 从对话中分析指定 Run |

Slash command 需要可发现，而不是要求用户背诵。OpenCode 的命令面板展示标题、描述、分类和快捷键，并把高频项放入 Suggested；Codex 的 `/` popup 以展示顺序表达频率，并显示每条命令描述；Claude Code 和 Gemini CLI 都支持输入 `/` 后过滤或用 `/commands`、`/skills` 查看来源（[OpenCode command palette](https://github.com/anomalyco/opencode/blob/fab213312927ea64cf968832c527206e8c944f9e/packages/tui/src/component/command-palette.tsx#L26-L78)、[Codex command popup](https://github.com/openai/codex/blob/56395bddaf26eb2829387ca6a417bf9128e5b239/codex-rs/tui/src/bottom_pane/command_popup.rs#L143-L220)、[Claude Commands](https://code.claude.com/docs/en/commands)、[Gemini Commands](https://github.com/google-gemini/gemini-cli/blob/acae7124bdd849e554eaa5e090199a0cf08cd782/docs/reference/commands.md#L110-L124)）。

## 最小落地顺序

1. 修正状态模型：拆开 `failed`、`crashed`、`cancelled`、`unknown`，禁止通过余数推导成功。
2. 让 `Skills` 计数可进入明细；返回每个 Skill 的 name、description、enabled、scope、location、discovery root。
3. 重做 Recent Runs：一条 Run 两行以内，只显示可决策信息。
4. 重做 Run Detail：先结构化 failure summary 和 log path，再显示 Agent 状态。
5. 删除默认 `Events` 和全量日志；增加折叠 timeline、独立 log pager、debug technical details。
6. 增加 `Ask AI to diagnose`，只向 TUI Agent提供最小诊断包。

验收标准不是“仍然能看到所有内部数据”，而是用户无需阅读日志就能回答：**什么失败了、为什么可能失败、证据在哪里、下一步该做什么**；需要深入时又能在一次操作内进入完整证据。

## 研究固定版本

- OpenCode：[`fab213312927ea64cf968832c527206e8c944f9e`](https://github.com/anomalyco/opencode/tree/fab213312927ea64cf968832c527206e8c944f9e)
- Codex CLI：[`56395bddaf26eb2829387ca6a417bf9128e5b239`](https://github.com/openai/codex/tree/56395bddaf26eb2829387ca6a417bf9128e5b239)
- Gemini CLI：[`acae7124bdd849e554eaa5e090199a0cf08cd782`](https://github.com/google-gemini/gemini-cli/tree/acae7124bdd849e554eaa5e090199a0cf08cd782)
- Claude Code：官方文档，访问于 2026-07-18；其客户端实现未作为开源源码引用。
