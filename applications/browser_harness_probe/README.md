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
    └── browser_harness_probe_agent.yaml
```

## Agent 与 Tool 分工

| 组件 | 职责 |
|---|---|
| `browser_harness_probe` Agent | 按 workflow 调用 doctor、isolated demo、real demo，并解释结果 |
| `browser_harness_doctor` Tool | 执行 `browser-harness --doctor` 并返回结构化 JSON |
| `run_isolated_demo_probe` Tool | 启动独立 Chrome profile，通过 `BU_CDP_URL` 调用 browser-harness 打开 GitHub 页面 |
| `run_real_demo_probe` Tool | 连接用户真实 Chrome，调用 browser-harness 打开 GitHub 页面 |
| `run_browser_harness_python` Tool | 为后续自定义 browser-harness Python 脚本预留 |
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
| `validate_application_yaml.py --app-root applications/browser_harness_probe` | 通过：`valid=true`，`files_checked=1`，`error_count=0` |
| `scan_app_structure('applications/browser_harness_probe')` | 通过：识别到 1 个 Supervisor、4 个 Tool、入口脚本和应用级 config 覆盖 |
| `quick_validate.py applications/browser_harness_probe/skills/browser-harness-agentloom` | 通过：`Skill is valid!` |
| `py_compile browser_harness_probe_app.py` | 通过 |
| `py_compile agent_tools/*.py` | 通过 |
| `loom create ... -o /tmp/browser_harness_probe_generated_app.py` | 通过，生成脚本可 `py_compile` |
| isolated Chrome Tool 直调 | 通过：`success=true`，打开 `https://github.com/browser-use/browser-harness`，返回 page_info |
| isolated Chrome AgentLoom 应用 | 通过：Agent 成功加载动态工具并调用 `browser_harness_doctor`、`run_isolated_demo_probe` |
| real Chrome Tool 直调 | 未通过：Tool 成功调用 browser-harness，但 Chrome 未启用 remote debugging，stderr 要求打开 `chrome://inspect/#remote-debugging` 并点击 Allow |
| real Chrome AgentLoom 应用 | 未通过：Agent 成功加载并调用 `run_real_demo_probe`；失败原因同上，是浏览器授权问题，不是 AgentLoom 工具注册失败 |

## 已知问题

- `real` 模式依赖用户真实 Chrome 的 remote debugging 授权。若 `browser-harness` 提示勾选 `chrome://inspect/#remote-debugging` 或点击 Allow，这是浏览器授权问题，不代表 AgentLoom Tool 注册失败。
- `isolated` 模式会打开一个独立 Chrome 窗口，使用缓存目录下的新 profile，不复用真实登录态。
- 干净 main worktree 不包含 ignored 的 `config/llm.yaml`。直接 `loom run` 或运行 Python wrapper 前，需要把本地 `config/llm.yaml` 放到该 worktree；否则会在模型调用前报 `Model type 'powerful' is not defined in config/llm.yaml`。
