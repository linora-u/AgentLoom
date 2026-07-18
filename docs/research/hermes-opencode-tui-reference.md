# Hermes Agent / OpenCode TUI 复用研究

> 结论先行：AgentLoom 不应把 Hermes 或 OpenCode 整仓嵌进来。最短路径是以 **OpenCode 的 TUI 外壳与交互模型**为主，按 MIT 许可提取少量、固定版本的 UI 原语；以 **Hermes 的 Sessions / Agents / Skills / Cron 交互**作为功能参考；所有项目、Agent、Run、对话和定时任务数据仍由 AgentLoom 自己的单一后端提供。这样可以最大化复用，同时避免两个运行时、两套会话库和两套配置成为新的复杂度来源。

本文只依据两个本地官方仓库的 README、源码、CLI `--help` 与实际 TUI 运行结果，不把截图印象或二手文章当证据。

## 1. 研究对象与可复现命令

### Hermes Agent

- 官方仓库：`git@github.com:NousResearch/hermes-agent.git`
- 本地版本：`29e3983fa879186b2122bd6779a2deb266f4acc5`
- 许可证：MIT（`hermes-agent/LICENSE`）
- CLI / TUI 安装与验证：

```bash
cd /Users/bytedance/code/data_clear/AgentLoom/hermes-agent
npm ci --workspace ui-tui --ignore-scripts
npm run --workspace ui-tui build
uv sync --frozen

.venv/bin/hermes --help
.venv/bin/hermes cron --help
.venv/bin/hermes skills --help

mkdir -p /tmp/agentloom-hermes-reference
stty cols 160 rows 45
HERMES_HOME=/tmp/agentloom-hermes-reference \
HERMES_TUI_STARTUP_TIMEOUT_MS=30000 \
.venv/bin/hermes --tui --safe-mode
```

实际构建成功：TUI bundle 约 3.3 MB，Python 环境使用 CPython 3.12.13，安装项目版本 0.18.2。为了不读取用户真实配置，运行时使用了临时 `HERMES_HOME` 和 `--safe-mode`；因此能验证完整 UI、会话/技能等 RPC，但没有验证真实模型回复。

### OpenCode

- 官方仓库：`git@github.com:anomalyco/opencode.git`
- 本地版本：`efb6cc2d4bf6332eb156709795d2b3a649198b65`
- 许可证：MIT（`opencode/LICENSE`）
- CLI / TUI 验证：

```bash
cd /Users/bytedance/code/data_clear/AgentLoom/opencode
bun install --frozen-lockfile --ignore-scripts

bun run --cwd packages/opencode --conditions=browser src/index.ts --help
bun run --cwd packages/opencode --conditions=browser src/index.ts session --help
bun run --cwd packages/opencode --conditions=browser src/index.ts agent --help

mkdir -p /tmp/agentloom-opencode-reference/{data,cache,config}
stty cols 160 rows 45
XDG_DATA_HOME=/tmp/agentloom-opencode-reference/data \
XDG_CACHE_HOME=/tmp/agentloom-opencode-reference/cache \
XDG_CONFIG_HOME=/tmp/agentloom-opencode-reference/config \
OPENCODE_DISABLE_AUTOUPDATE=1 \
bun run --cwd packages/opencode --conditions=browser src/index.ts \
  /Users/bytedance/code/data_clear/AgentLoom --pure
```

`bun install` 安装了 4,660 个包，但有 13 个与本次 TUI 路径无关的 tarball 解压/完整性失败，因此这里不声称整仓生产构建通过；官方 TypeScript 入口、CLI help、TUI、会话创建和一次真实模型对话均实际运行成功。

## 2. 实际 TUI 观察

### Hermes：功能中心型 TUI

启动先显示动态状态 `summoning hermes…`、`forging session…` 和秒数。临时配置没有 provider 时，约 38 秒后显示 `Setup Required`，等待期间动画持续、界面可响应，并没有冻结。

实测交互：

- `/help` 展示 `/sessions`、`/skills`、`/plugins`、`/details` 等命令，以及补全、历史、换行、shell 命令和中断快捷键。
- `Ctrl+X` 打开 Sessions：先显示 `loading sessions…`，随后显示 `1 live · 0 resumable`、固定的 `+ new`、当前会话模型和 `idle` 状态；支持切换、新建、刷新和关闭。
- `/skills` 先显示 `loading skills…`，随后打开可键盘浏览的 Skills Hub，按分类显示技能与数量，并支持查看/安装。
- slash command 有自动补全：第一次 Enter 可能是接受补全，第二次才执行。这是源码/实测一致的输入语义，但 AgentLoom 不应照搬这一点，主输入框应保证 Enter 始终发送，命令补全用 Tab。

README 明确把 TUI、多行输入、slash command、历史、流式输出、session search、skills、cron 和 subagents 都作为正式能力；CLI help 也暴露了 `sessions`、`skills`、`cron`、`status` 等入口。对应证据见 `hermes-agent/README.md:19-28,145-155` 与 `hermes-agent/hermes_cli/_parser.py:44-45`。

### OpenCode：对话优先型 TUI

启动即出现 Logo、居中 composer、Agent / Model / Provider 行，以及 `tab agents`、`ctrl+p commands` 和项目/分支信息。实测：

- `Ctrl+P` 打开可搜索命令面板，包含 New/Switch session、模型、provider、状态、主题、帮助与退出。
- Switch session 在空临时状态中立即打开，显示 `No results found`；仍提供 pin、delete、rename 快捷键。
- Tab 在 composer 中直接切换 `Build → Plan → Build`，无额外弹窗。
- 输入 `Reply with only OK.` 后按 Enter，OpenCode 创建会话、自动命名为 `Quick acknowledgment`，右侧出现 Session ID、Context、tokens、百分比、cost 和 LSP；底部显示块状动态波形及 `Thinking`，随后显示 `Thought: 101ms`，约 6.1 秒得到 `OK`。
- 在 160 列宽度下右侧详情栏自动出现，主对话区仍保留足够宽度。

这证明 OpenCode 的默认交互是“模型真的回答 + 状态/成本/上下文同步更新”，不是只把自然语言转换成 YAML。AgentLoom 的 Builder 对话也应保持这一性质：它可以创建/修改 Agent YAML，但普通问题必须正常回答。

## 3. 源码层面的可复用能力

### 会话、增量状态与缓存

OpenCode 的关键不是某个面板，而是一条统一的数据流：

- `opencode/packages/tui/src/context/sync.tsx:144-168` 用集合/Map 记录正在加载、已经加载的 session，并限制默认会话时间窗口。
- 同文件 `:170-440` 订阅事件，把 `session.updated`、status、message、part 和 `part.delta` 直接归并到本地 store；消息只保留最近 100 条。
- 同文件 `:445-533` 把启动拆成阶段：provider/agent/config/project 是首屏依赖；session、commands、LSP、MCP、resource、auth、VCS 等在后台并行补齐。
- 同文件 `:588-659` 的 `session.sync` 会去重并行请求，懒加载 session/messages/todo/diff，并把加载期间收到的 live event 合并回来，避免旧响应覆盖新状态。

Hermes 的 `hermes-agent/ui-tui/src/components/activeSessionSwitcher.tsx:310-443` 采用相同原则：原始 history 有缓存，1.5 秒的 live status 轮询不会重复查询历史库；刷新失败时保留最后一次成功数据。`:530-675` 还包含鼠标行选择、键盘控制、loading 与 windowed list。

可复用结论：AgentLoom 应复用这个“**summary 快照 + cursor event + 按需 hydrate**”模式，而不是每次点击 Agent 或 Run 都重新扫描整个项目。

### 长列表、滚动与详情栏

- Hermes `ui-tui/src/hooks/useVirtualHistory.ts` 缓存行高、在宽度变化时按比例估算，并分时重新测量；`components/appLayout.tsx:180-260` 用上下 spacer 只渲染当前 history slice。
- OpenCode `packages/tui/src/routes/session/sidebar.tsx:12-103` 是固定 42 列、带垂直 scrollbar 的 ScrollBox，内容由 title/workspace/share、插件扩展区和 footer 组成。
- OpenCode `packages/tui/src/routes/session/index.tsx:1165-1310` 根据宽度把主会话与 sidebar 组合，并把权限、问题、subagent footer 和 prompt 保持在稳定位置。

可复用结论：不要把 78 个 Agent 永久塞进窄左栏。主视图保持对话；`Sessions / Agent Systems / Runs` 用可搜索的 overlay/command palette 进入。宽屏时才显示选中实体的详情栏，并保留完整定义、Run、子任务、Skills、错误与产物信息——删除的是无关代码工具，不是 Agent 详情。

### 输入、加载与动画

- OpenCode `packages/tui/src/component/prompt/index.tsx:1302-1550` 包含 fade-in、动画帧、textarea、提交、粘贴、IME、agent/model/provider 行以及 retry 状态；spinner 在 `component/spinner.tsx:19` 以 80ms 更新。
- `component/startup-loading.tsx:5-63` 延迟 500ms 才显示 spinner，区分 `Loading plugins` 和 `Finishing startup`，并设置最小展示时间，避免一闪而过。
- Hermes `tui_gateway/server.py:172-230` 把可能阻塞的 RPC 放入线程池，源码注释明确提到 git/path 调用可能阻塞数秒；目的是不让 event loop、动画和轮询卡顿。

可复用结论：动画只能反映真实阶段，不能掩盖同步全量扫描。AgentLoom 首帧先展示缓存 summary；后台逐步加载 definitions/runtime/details；加载超过 500ms 才显示阶段与耗时；旧选项的请求必须可取消。

### Agents / Subagents 与执行状态

- Hermes `ui-tui/src/components/agentsOverlay.tsx:594-675` 合并 live subagents 与历史快照，短暂保留刚结束的 Agent 以避免一帧闪烁，并以 300/500ms 节奏更新。
- 同文件 `:680-951` 支持 delegation status、interrupt、累计统计、finished replay 和历史导航。
- `ui-tui/src/app/createGatewayEventHandler.ts:125-150,846-945` 把 spawn tree 持久化，同时以内存 live state 为权威处理 subagent event。
- OpenCode `packages/tui/src/routes/session/subagent-footer.tsx:11-131` 显示 parent/sibling、usage、tokens、cost，并支持 Parent / Prev / Next 导航。
- OpenCode `packages/tui/src/routes/session/index.tsx:2213-2285` 懒加载 child session，组合工具调用、当前状态、耗时和 summary；`packages/opencode/src/session/status.ts:13-54` 通过事件发布 working/retry/idle 状态。

可复用结论：AgentLoom 要同时提供“当前项目所有 Agent/Run 的 summary”和“点开后的完整执行树”。运行中状态走事件；历史详情直接按 ID 读取。列表不应靠高频全量扫描刷新。

### Skills 与 Cron

- Hermes Skills Hub 的 list/inspect/install 分别在 `ui-tui/src/components/skillsHub.tsx:23-31,70-79,181`；插件也有对应 loading/error/list 状态（`components/pluginsHub.tsx:50-90,152-171`）。
- Hermes CLI 实测 `skills` 支持 browse/search/install/inspect/list/check/update/audit/config；`cron` 支持 list/create/edit/pause/resume/run/remove/status/runs/tick。
- Hermes gateway 的 cron RPC 在 `tui_gateway/server.py:14987-15009`，skills/cron/plugins/browser 等方法统一挂在 gateway 上；cron 的持久化实现位于 `hermes-agent/cron/jobs.py` 与 `cron/executions.py`。
- OpenCode 的 Skill dialog 在 `packages/tui/src/component/dialog-skill.tsx:13-69`，通过 resource 获取技能并提供可搜索选择。

可复用结论：交互可以参考、少量 UI 可以提取，但不应把 Hermes cron daemon 作为 AgentLoom 的子进程。那会引入第二套 provider 配置、任务库、进程生命周期和故障恢复。AgentLoom 应保有自己的 durable schedule 与 skill catalog，并通过同一 workbench backend 暴露。

## 4. 直接复用边界

### 建议提取（固定上游 commit，并保留 MIT 归属）

以 OpenCode 为主：

1. Prompt 的布局和小型交互原语：textarea、IME/paste、agent/model/provider 状态行、thinking/retry 动画。
2. Command palette / searchable dialog / session list 的交互结构。
3. Spinner、宽屏 sidebar、scrollbox 与快捷键提示等无业务状态组件。
4. `sync.tsx` 的分阶段启动、事件 reducer、lazy hydration 与请求去重思路；需改写成 AgentLoom DTO，不直接依赖 OpenCode server context。

以 Hermes 为补充：

1. Sessions overlay 的 `+ new`、live/resumable 分组、quiet refresh 和“失败保留旧数据”行为。
2. Agents overlay 的 live + history 合并、执行树、finished replay、interrupt 交互。
3. Skills Hub 和 Cron 管理的页面信息结构。

建议在 `third_party/` 保存提取代码、上游 commit、原始 LICENSE 和本地改动说明；不要复制后删掉来源信息。

### 不建议直接复用

- 不 fork 整个 OpenCode：它的 TUI 组件依赖内部 Solid context、session/message/tool/MCP/LSP 等完整编码产品模型，删功能仍会留下庞大升级面。
- 不启动 Hermes Python gateway：AgentLoom 已有 Python bridge，再接一层会产生双重 session/run/cron 真相。
- 不复制 OpenCode/Hermes 的 Agent 执行引擎：AgentLoom 的差异化是创建、查看与运行自己的 Agent 系统，不是再实现一个 coding agent。
- 不保留 coding 专属能力：file diff、LSP、MCP coding tools、shell-first workflow、代码权限弹窗可以删除；Agent definition、model、skills、runs、children、logs、artifacts、cost、duration 和 failure detail 必须保留。

## 5. “最大化直接复用”接口设计

本变体选择“**提取 OpenCode UI 原语 + AgentLoom adapter**”。整个 UI 只依赖一个深模块 `WorkbenchBackend`；模块内部隐藏文件扫描、模型 SDK、YAML、Run 观察、Cron、Skills 与缓存。顶层只有三个方法，避免把两个上游项目的内部对象泄露到页面。

```ts
type EntityRef =
  | { kind: "conversation" | "agent" | "application" | "run" | "schedule"; id: string }
  | { kind: "task"; runID: string; taskID: string }

type DetailSection =
  | "definition"
  | "topology"
  | "models"
  | "skills"
  | "runs"
  | "children"
  | "timeline"
  | "logs"
  | "artifacts"
  | "result"

type WorkbenchSnapshot = {
  cursor: string
  project: ProjectSummary
  models: ModelSummary[]
  defaultModel?: string
  conversations: ConversationSummary[]
  agents: AgentSummary[]
  applications: ApplicationSummary[]
  runs: RunSummary[]
  schedules: ScheduleSummary[]
  skills: SkillSummary[]
}

type WorkbenchCommand =
  | { type: "chat.send"; conversationID?: string; text: string; model?: string }
  | { type: "chat.cancel"; conversationID: string }
  | { type: "conversation.new"; model?: string }
  | { type: "entity.hydrate"; ref: EntityRef; sections: DetailSection[] }
  | { type: "agent.validate"; proposal: AgentProposal }
  | { type: "agent.apply"; proposal: AgentProposal }
  | { type: "agent.run"; agentID: string; input?: unknown }
  | { type: "run.stop"; runID: string }
  | { type: "schedule.create"; spec: ScheduleSpec }
  | { type: "schedule.update"; scheduleID: string; patch: Partial<ScheduleSpec> }
  | { type: "schedule.pause" | "schedule.resume" | "schedule.runNow" | "schedule.delete"; scheduleID: string }
  | { type: "skill.install" | "skill.remove" | "skill.enable" | "skill.disable"; skillID: string }
  | { type: "refresh"; ref?: EntityRef }

type WorkbenchEvent = {
  seq: number
  cursor: string
  at: string
  type:
    | "chat.delta"
    | "chat.completed"
    | "chat.error"
    | "entity.patch"
    | "load.progress"
    | "run.status"
    | "run.log"
    | "run.tool"
    | "child.updated"
    | "schedule.updated"
    | "toast"
  payload: unknown
}

interface WorkbenchBackend {
  bootstrap(input: {
    projectRoot: string
    preferCache?: boolean
  }): Promise<WorkbenchSnapshot>

  dispatch(
    command: WorkbenchCommand,
    signal?: AbortSignal,
  ): Promise<{ accepted: boolean; operationID?: string }>

  events(cursor?: string): AsyncIterable<WorkbenchEvent>
}
```

典型用法：

```ts
const snapshot = await backend.bootstrap({
  projectRoot,
  preferCache: true,
})

store.replace(snapshot)
void consume(async () => {
  for await (const event of backend.events(snapshot.cursor)) {
    store.reduce(event)
  }
})

// 普通对话会流式回答；需要时也可产出 Agent proposal。
await backend.dispatch({
  type: "chat.send",
  text: "这个 Agent 为什么失败？",
  model: selectedModel,
})

// 用户点开 Agent 后才补齐完整信息，切换选择时可取消旧请求。
const controller = new AbortController()
await backend.dispatch(
  {
    type: "entity.hydrate",
    ref: { kind: "agent", id: selectedAgentID },
    sections: ["definition", "runs", "children", "skills", "timeline"],
  },
  controller.signal,
)
```

模块内部必须隐藏：

- `config/llm.yaml` 的解析、模型能力映射、provider SDK 和各家流式事件归一化。
- 对话存储、tool/ReAct 步骤、Agent proposal 校验和 YAML 原子写入。
- 项目索引、文件指纹、watcher 合并、summary cache、详情分页与请求取消。
- 本地进程/Run 的发现，manifest、event、log、artifact 与子任务树的统一状态。
- event cursor 的排序、去重、断线恢复、过期 reset 和旧响应保护。
- durable schedule 的租约、重试、错过触发、执行记录与恢复。
- skill catalog、安装、启停、版本和安全边界。
- 提取的 OpenCode/Hermes 代码与上游版本之间的兼容和许可证归属。

主要权衡：

- 优点：页面可以直接复用成熟的 OpenCode 交互部件，业务状态仍只有 AgentLoom 一个真相；以后替换模型 SDK、Run 存储或 scheduler 不需要改 UI。
- 代价：OpenCode 内部组件并非稳定公共包，提取后需要维护小型 vendor patch，并主动跟踪安全和许可证变化。
- 约束：只提取窄 UI 部件，禁止让页面 import OpenCode server/session 类型或 Hermes gateway DTO；否则 adapter 会退化成第二套业务模型。

## 6. 对 AgentLoom 的落地判断

最终界面应是“对话工作台”，而不是 Codex/OpenCode 的另一个通用 coding agent：

1. 默认首页是简单、可靠的对话框，Enter 发送，Tab/命令面板负责切换 Agent/模型与补全。
2. `Ctrl+P` 统一进入 Sessions、Agent Systems、Runs、Schedules、Skills 和模型选择。
3. 项目内所有 Agent / Run 以搜索 overlay 展示；点击后在宽屏详情栏或窄屏独立页面显示完整状态和执行树。
4. Builder 对话通过 ReAct 读取项目并提出 Agent YAML proposal；用户显式 Apply 后才写入。它也能正常回答问题，不被限制成 YAML 生成器。
5. 首帧依赖轻量 snapshot；状态通过 cursor event 增量更新；详情按 section 懒加载。任何 loading/动画都对应真实阶段。

这条边界既保留了用户真正需要的“创建 Agent + 看全项目 Agent/Run 状态 + 点开完整详情 + Skills/Cron”，又删除了 coding agent 的无关功能，不会因为复用开源项目而把产品重新做成 OpenCode。
