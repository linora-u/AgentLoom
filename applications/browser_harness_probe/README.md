# Browser Harness Probe

## 目标

验证 AgentLoom 能否通过普通 Python Tool 调用外部 `browser-harness` CLI 控制浏览器。

这个 Application 是集成探针，不是完整生产级浏览器 Agent。

## 输入与输出

| 项 | 说明 |
|---|---|
| 输入 | 用户要求验证 isolated Chrome、real Chrome 或两者都验证 |
| 输出 | Markdown 总结，包含 `browser-harness --doctor`、隔离 Chrome demo、真实 Chrome demo 的 JSON 结果 |

## 组织结构

```text
applications/browser_harness_probe/
├── README.md
├── browser_harness_probe_app.py
├── zsxq_scraper_app.py
├── agent_tools/
│   ├── __init__.py
│   └── browser_harness_tools.py
├── config/
│   └── system.yaml
├── skills/
│   └── browser-harness-agentloom/
│       ├── SKILL.md
│       └── agents/
│           └── openai.yaml
└── workflows/
    ├── browser_harness_probe_agent.yaml
    └── zsxq_scraper_agent.yaml
```

## Agent 与 Tool 分工

| 组件 | 职责 |
|---|---|
| `browser_harness_probe` Agent | 按 workflow 调用 doctor、isolated demo、real demo，并解释结果 |
| `zsxq_owner_post_scraper` Agent | 复用用户已打开的真实 Chrome 标签，滚动并抓取知识星球楼主帖，落 CSV |
| `browser_harness_doctor` Tool | 执行 `browser-harness --doctor` 并返回结构化 JSON |
| `run_isolated_demo_probe` Tool | 启动独立 Chrome profile，通过 `BU_CDP_URL` 调用 browser-harness 打开 GitHub 页面 |
| `run_real_demo_probe` Tool | 连接用户真实 Chrome，调用 browser-harness 打开 GitHub 页面 |
| `run_browser_harness_python` Tool | 为后续自定义 browser-harness Python 脚本预留 |
| `scrape_zsxq_owner_posts` Tool | 在用户真实 Chrome 中定位/激活 zsxq 标签，从最新滚到 `since_date`，展开折叠内容，按楼主过滤并写 CSV |
| `browser-harness-agentloom` Skill | 沉淀后续 Agent 做 browser-harness + AgentLoom 集成/排障时应复用的执行经验 |

`config/system.yaml` 写 `skills: []`，用于关闭全局自动发现 Skills，避免 probe 运行时被无关 Skill Hook 或缺失工具校验干扰。

README 记录本次验收事实；`skills/browser-harness-agentloom/SKILL.md` 记录后续执行手册。后续遇到 browser-harness doctor、isolated/real Chrome、`config/llm.yaml`、Tool 注册等问题时，优先读取该 Skill。

## 安装

按 browser-harness 官方建议使用外部 editable CLI，不把它加入 AgentLoom 的 `pyproject.toml` 或 `uv.lock`：

```bash
git clone https://github.com/browser-use/browser-harness /Users/bytedance/code/browser-harness
uv tool install -e /Users/bytedance/code/browser-harness
command -v browser-harness
```

参考：

- https://github.com/browser-use/browser-harness/blob/main/install.md
- https://github.com/browser-use/browser-harness/blob/main/SKILL.md

## 运行

默认入口是直接运行 Agent YAML；这也是最小路径，不依赖 `browser_harness_probe_app.py`：

```bash
.venv/bin/loom run applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml
```

`loom run` 会使用 YAML 的 `description` 作为任务；本 Application 的默认任务是先跑 doctor，再按 isolated -> real 的顺序验证两个 demo。

`browser_harness_probe_app.py` 不是运行所必需。当前保留它只做便利 wrapper：当需要传入自定义自然语言请求、`log_to_file` 或 `resume` 时，它会调用 `run_app(..., task_override=...)`。

通过 wrapper 默认同时验证 isolated Chrome 和 real Chrome：

```bash
.venv/bin/python applications/browser_harness_probe/browser_harness_probe_app.py
```

只验证隔离 Chrome：

```bash
.venv/bin/python applications/browser_harness_probe/browser_harness_probe_app.py \
  "Run browser-harness doctor and verify isolated Chrome only."
```

只验证真实 Chrome：

```bash
.venv/bin/python applications/browser_harness_probe/browser_harness_probe_app.py \
  "Run browser-harness doctor and verify real Chrome only."
```

## 抓取知识星球楼主帖（zsxq scraper）

新增的 `zsxq_owner_post_scraper` Agent 复用用户已经登录、并已打开目标 zsxq 群组的 Chrome 标签（默认 `https://wx.zsxq.com/group/51111541884844`），自最新帖向下滚动，展开折叠内容，过滤出楼主发的内容，写入 `zsxq_owner_posts.csv`（位于项目根目录），列为 `时间, 内容, 超链接`。

前置条件：

- 用户的真实 Chrome 已启用 remote debugging（按 `chrome://inspect/#remote-debugging` 勾选 "Allow remote debugging for this browser instance" 并点 Allow）。
- 用户已在 Chrome 中登录知识星球，并打开了目标群组页签。
- `browser-harness` 已通过 `uv tool install -e /Users/bytedance/code/browser-harness` 安装到 PATH。

运行：

```bash
# 直接运行 Agent YAML（推荐）
.venv/bin/loom run applications/browser_harness_probe/workflows/zsxq_scraper_agent.yaml

# 或通过 wrapper 传自定义自然语言请求 / log / resume
.venv/bin/python applications/browser_harness_probe/zsxq_scraper_app.py
```

`scrape_zsxq_owner_posts` 默认参数（在 YAML `fixed_args` 中固定，LLM 无法覆盖）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `group_url` | `https://wx.zsxq.com/group/51111541884844` | zsxq 群组 URL |
| `since_date` | `2024-01-01` | 早于该日期的帖子被丢弃，遇到即停止滚动 |
| `csv_path` | `zsxq_owner_posts.csv` | 相对于 AgentLoom 项目根 |
| `max_scrolls` | `300` | 滚动轮上限 |
| `scroll_pause_seconds` | `1.4` | 每轮滚动后等待 |
| `stall_limit` | `8` | 连续多少轮无新增即停 |
| `timeout_seconds` | `1500` | browser-harness 子进程硬超时 |

楼主名留空时由脚本自动检测——取页面上第一个非空作者名作为楼主；如果用户群里第一张卡片不是楼主，可以临时直接调用工具函数并显式传 `owner_name`：

```bash
.venv/bin/python - <<'PY'
from applications.browser_harness_probe.agent_tools.browser_harness_tools import scrape_zsxq_owner_posts
print(scrape_zsxq_owner_posts(owner_name="<显示名>"))
PY
```

## 浏览器模式

| 模式 | 说明 |
|---|---|
| `isolated` | Tool 启动独立 Chrome profile，设置 `BU_CDP_URL=http://127.0.0.1:<port>`，profile 位于用户缓存目录，不写入 repo |
| `real` | Tool 连接用户正在使用的 Chrome，不启动新浏览器；如果 Chrome 未授权 remote debugging，需要打开 `chrome://inspect/#remote-debugging` 并点击 Allow |

## 验证记录

| 命令 | 结果 |
|---|---|
| `git status --short` | 通过：新增 `applications/browser_harness_probe/`；`config/llm.yaml` 为 ignored 本地环境文件 |
| `.venv/bin/loom --help` | 通过 |
| `.venv/bin/loom run --help` | 通过：确认可直接运行 Agent YAML |
| `.venv/bin/loom run applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml` | 环境阻塞：当前 main worktree 缺 ignored 的 `config/llm.yaml`，运行在模型调用前失败，错误为 `Model type 'powerful' is not defined in config/llm.yaml` |
| `command -v browser-harness` | 通过：`/Users/bytedance/.local/bin/browser-harness` |
| `browser-harness --doctor` | 部分通过：Chrome running ok；daemon/active connections fail 是未建立持久连接的初始状态；`profile-use` 和 `BROWSER_USE_API_KEY` 是 cloud/profile sync 可选项 |
| `validate_application_yaml.py --app-root applications/browser_harness_probe` | 通过：`valid=true`，`files_checked=2`，`error_count=0`（新增 `zsxq_scraper_agent.yaml` 后） |
| `scan_app_structure('applications/browser_harness_probe')` | 通过：识别到 2 个 Supervisor、5 个 Tool、两个入口脚本、应用级 config 覆盖 |
| `quick_validate.py applications/browser_harness_probe/skills/browser-harness-agentloom` | 通过：`Skill is valid!` |
| `py_compile browser_harness_probe_app.py` | 通过 |
| `py_compile zsxq_scraper_app.py` | 通过 |
| `py_compile agent_tools/*.py` | 通过 |
| `loom create .../browser_harness_probe_agent.yaml -o /tmp/...` | 通过，生成脚本可 `py_compile` |
| `loom create .../zsxq_scraper_agent.yaml -o /tmp/...` | 通过，生成脚本可 `py_compile` |
| `scrape_zsxq_owner_posts` 模板/解析/事件 单测 | 通过：脚本模板替换后可 `compile()`，`_parse_final_json` 与 `_collect_events` 行为正确 |
| isolated Chrome Tool 直调 | 通过：`success=true`，打开 `https://github.com/browser-use/browser-harness`，返回 page_info |
| isolated Chrome AgentLoom 应用 | 通过：Agent 成功加载动态工具并调用 `browser_harness_doctor`、`run_isolated_demo_probe` |
| real Chrome Tool 直调 | 未通过：Tool 成功调用 browser-harness，但 Chrome 未启用 remote debugging，stderr 要求打开 `chrome://inspect/#remote-debugging` 并点击 Allow |
| real Chrome AgentLoom 应用 | 未通过：Agent 成功加载并调用 `run_real_demo_probe`；失败原因同上，是浏览器授权问题，不是 AgentLoom 工具注册失败 |
| zsxq scraper 真实跑通 | 待用户运行：需用户先在 Chrome `chrome://inspect/#remote-debugging` 勾选 Allow，并保持 zsxq 群标签打开；满足后 `.venv/bin/loom run applications/browser_harness_probe/workflows/zsxq_scraper_agent.yaml` 即可 |

## 已知问题

- `real` 模式依赖用户真实 Chrome 的 remote debugging 授权。若 `browser-harness` 提示勾选 `chrome://inspect/#remote-debugging` 或点击 Allow，这是浏览器授权问题，不代表 AgentLoom Tool 注册失败。
- `isolated` 模式会打开一个独立 Chrome 窗口，使用缓存目录下的新 profile，不复用真实登录态。
- 干净 main worktree 不包含 ignored 的 `config/llm.yaml`。直接 `loom run` 或运行 Python wrapper 前，需要把本地 `config/llm.yaml` 放到该 worktree；否则会在模型调用前报 `Model type 'powerful' is not defined in config/llm.yaml`。
