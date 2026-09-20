# 13：安装与发行验收

起始 main：`2ec1412c`。最终同步已交付 11 的 main `87d4a287`；发行与真实模型验收候选为
`0b5279c4`，生成发行物时工作区干净。后续仅把 Python package-data 的 schema 文件名
匹配拓宽为通配，并实际构建当前/重命名 schema 两种 wheel 验证；执行代码和依赖未变。
完整证据摘要见 [13-validation.json](13-validation.json)，安装命令见
[Runtime installation](../../en/runtime_installation.md)。

## 交付内容

- Python extras 分离为 `pi`、`smol`、`code`。Pi 的最小安装没有 smol SDK、
  smol instrumentation、AST/Serena/Go/Node 等专业工具依赖。`code` 按需增加
  专业工具；tree-sitter/Bash 留在公共依赖中，服务跨基座 Shell 治理。
- 原 `./install` 显式安装 `smol + code`，保持原应用环境；新增
  `./install --runtime pi`，由 uv 安装 Python 包，再自动执行 Pi 安装器。
  更新时保留实际安装目录和 runtime profile，包括首次用临时环境变量指定目录的情况。
- Pi SDK 固定为 `@earendil-works/pi-coding-agent@0.79.4`，由现有 npm lock
  下载到安装包的 `adapters/pi/bridge/node_modules/`。仓库和 Python 发行物
  只带 AgentLoom 自有 bridge 源码、schema、manifest/lock，不携带上游 SDK 或 dist。
- 安装就绪检查覆盖全部 bridge TypeScript 源码与对应 JS assets。源码、schema、
  lock 变化，SDK 缺失或构建产物缺失时，运行入口在启动 SDK 前明确提示重新安装。
  Application 执行本身不做临时 npm 安装。
- 无 smol 环境的 CLI help、失败分类与缺依赖提示不再依赖 smol 导入。
  10 的取消验证又发现 LiteLLM 冷导入会触发 cost-map 联网；追加 `fdff67c7`
  使用 runtime provider/retryable 合同及已加载 SDK 异常类型，保留拒绝优先和异常链规则。
- 发行构建从 `uv.lock` 导出含传递依赖和 hashes 的 build constraints，
  `uv build --require-hashes --build-constraints ...` 先构建 sdist，再由 sdist 构建 wheel。
  本票未修改 Pi 链所有者的 Node manifest/lock。

## 已执行的 Application 验证

验收在项目外三个新建环境中进行：锁定依赖、非 editable wheel、Python `-I`，
确认导入路径为各自的 site-packages，不存在 `src` 源码回退。Node 固定为 22.19.0。

| 环境 | 结果 | 覆盖 |
| --- | --- | --- |
| Pi | 13/13 | 安装前缺 SDK、旧 bridge、缺 asset、help、缺 YAML、缺 smol、无工具、原生 read、MCP、记忆、Goal、provider failure、SDK 子进程失败 |
| Pi + code | 9/9 | 安装/readiness 三项，无工具、原生 read、Python/TypeScript 大纲、AST、LSP tree-sitter fallback |
| smol + code | 7/7 | help、缺 YAML、read_file、旧 YAML、Python 大纲、MCP、记忆 |

29 项采用受控 HTTP 模型响应，真实运行官方 SDK、Application、工具、审计和资源清理。
smol 的旧 YAML 直接复制 `applications/test_demo/workflows/test_todo_off_agent.yaml`，
逐字节/SHA 校验未修改，保留默认工具集；结果为 `TODO_OFF_OK MOOL-4`。
MCP 检查真实查询结果与进程关闭；子进程失败检查实际被终止的 SDK PID。

另在同批发行环境执行了 3 次真实模型验证，均成功：Pi 的 `openai_chat` 和
`openai_responses` 分别调用官方 `read`，smol 调用 `read_file`，均返回实际文件标记。
真实模型配置只存在项目外受限权限的验证目录，不进入 Git 或发行物。

从同一个 sdist 和锁定 build constraints 再构建的 wheel 与首次 wheel 字节一致；
两次 SHA-256 均为 `cb15b9e4675117bf186994178f40cdf19ef3320e1dbd88a7894a83733947f15c`。
wheel/sdist 均不含 SDK、node_modules、bridge dist 或安装标记，并包含 56 个查询资源。

## 回归与审查

- 基础实现完整回归：**4412 passed、1 skipped**（443.62 秒，`07dfbe7`）；第三方 warnings 为 3。
- 此后追加 CLI 修复：42 passed；与已交付 11 组合的混合应用/记忆/CLI：95 passed。
  后续补丁按影响范围验证，未把此前全量数字冒充最新组合树的全量重跑。
- 安装器：3 passed，29 assertions；包含自定义安装目录、无环境变量的更新验证。
- 最后 Python packaging/SDK 安装：12 passed，含真实构建的 schema 改名场景；依赖固定与 Run context：17 passed。
- Pyright：5 文件，0 errors / 0 warnings。
- Standards 与 Spec 两路审查：构建依赖未锁、专业工具默认安装及自定义目录更新
  问题均已复现并修复；追加错误分类、schema 通配与 package-data 补丁也已复审。
  最终 Standards 硬问题/判断性问题均为 0，Spec 剩余问题为 0。

首次完整测试的唯一失败是旧测试从基础 dependencies 寻找 smol SDK；现在检查
`optional-dependencies.smol`，保留 `==1.26.0` 和实际安装版本断言，不降低保护。
初轮构建与失败日志保留在项目外证据目录。

## 下游与交付边界

13 消费 04/09，不等待 10/12，也不声明后两票能力已完成。14 必须在包含
10/12 的最终提交上重新构建发行物、重新运行 `tests/packaging/validate_profiles.py`，
不能把本票旧候选结果当作最终集成验收。10 已计划将 Pi wire/schema 升至 v2；
本票已消除 schema v1 文件名硬编码，但没有把尚未合入的 v2 协议当作已验证能力。

CI 已配置 `pi / pi-code / smol` 三环境矩阵和明确 extras；未向远端推送或触发
GitHub Actions，本地执行了同等安装验收。LSP 验证只覆盖 tree-sitter fallback，
不能据此宣称真实 language server 已验收。
