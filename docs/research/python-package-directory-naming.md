# 开源 Python Agent 项目的目录命名

日期：2026-09-23

范围：核对六个项目在指定提交的源码目录。这里只记录所列目录的可见名称，不把样本当作 Python 项目的统计调查。

## 源码观察

| 项目 | 实际目录和配置命名 | 对 `app` / `config` 的参考价值 |
| --- | --- | --- |
| [CrewAI](https://github.com/crewAIInc/crewAI/tree/65361144519a25bad55c5b1651d8a9d583166d01/lib/crewai/src/crewai) | 包根有 `agent/`、`agents/`、`flow/`、`memory/`、`tools/`，配置文件为 `settings.py`。 | 用简短的完整业务词；包根没有 `cnf/`。 |
| [LangGraph](https://github.com/langchain-ai/langgraph/tree/bdb85b5aa87a21de68371d2e534b81aeed398f57/libs/langgraph/langgraph) | 包根有 `channels/`、`graph/`、`managed/`、`pregel/`，配置文件为 `config.py`。 | `config` 是实际采用的简称。 |
| [AutoGen Core](https://github.com/microsoft/autogen/tree/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-core/src/autogen_core) 与 [AgentChat](https://github.com/microsoft/autogen/tree/027ecf0a379bcc1d09956d46d12d44a3ad9cee14/python/packages/autogen-agentchat/src/autogen_agentchat) | Core 有 `memory/`、`models/`、`tools/`、`code_executor/` 和 `_component_config.py`；AgentChat 有 `agents/`、`teams/`、`tools/`。 | 缩短的是常见词，复合概念仍写清楚。 |
| [Pydantic AI](https://github.com/pydantic/pydantic-ai/tree/ad0727bb568580992afebe77351702f8817e5476/pydantic_ai_slim/pydantic_ai) | 包根有 `agent/`、`models/`、`providers/`、`toolsets/`、`ui/`，配置文件为 `settings.py`。 | 使用短而能直读的词。 |
| [Dify API](https://github.com/langgenius/dify/tree/d3d3248e3be58f9292470662592390f9931e785a/api) | API 根有 [`configs/`](https://github.com/langgenius/dify/tree/d3d3248e3be58f9292470662592390f9931e785a/api/configs)、`controllers/`、`core/`、`services/` 和 `app.py`；[`core/app/`](https://github.com/langgenius/dify/tree/d3d3248e3be58f9292470662592390f9931e785a/api/core/app) 内含 `app_config/`、`apps/`、`features/`、`workflow/`。 | `app` 可以是明确的应用领域目录；配置代码较多时用 `configs/`。`app.py` 则是入口文件，含义不同。 |
| [Open WebUI](https://github.com/open-webui/open-webui/tree/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui) | 包根有 `models/`、`routers/`、`retrieval/`、`tools/`，以及 `config.py`、`main.py`。 | 配置代码集中时，一个 `config.py` 即可。 |

上述六个指定目录没有使用 `cnf/`，这只说明该样本的命名选择，不能推出所有开源项目都不用它。Python [PEP 8 的包与模块命名规则](https://peps.python.org/pep-0008/#package-and-module-names)要求包名短、全小写，并不要求把可读单词缩到最少字符。

## 对 AgentLoom 的建议（判断，不是外部事实）

- `src/application/` → `src/app/`：可行。当前目录包含运行、工厂、生命周期、CLI、Studio 等应用编排代码；`app` 足以表达这个边界。只有在团队刻意把 `application` 当作特定架构层术语时，才保留全称。
- `src/configuration/` → `src/config/`：推荐。比 `configuration` 短，也比 `cnf` 易读；现有加载器、默认值、校验和配置模型仍能归入同一目录。
- 不建议 `cnf/`：它节省三个字符，却增加阅读时的解码成本。样本中可见的是 `config.py`、`configs/` 和 `settings.py`，没有需要把 `config` 再缩写的证据。
- 不要机械缩短每个目录。`execution/`、`integrations/`、`runtimes/` 等是否更名，应由各自承担的职责决定；短名如果扩大歧义，就没有收益。

重命名属于 Python import 路径变更。实际实施时要一并更新源码导入、测试、脚本、文档和对外导入兼容需求。
