# main 迁移残留清理

维护者要求直接在 main 删除已经迁移、没有用途的文件。检查起点为 `074698556016c93dd6783778c23de125610aee6f`；本次只清理多余路径和重复资源，保留现有 YAML、builtin 名称及仍有消费者的兼容入口。

## 删除范围

| 删除的旧路径（相对 `src/`） | 现有实现及调用者处理 |
| --- | --- |
| `adapters/smolagents/models/litellm_retry.py` | 无消费者；重试实现仍在 `adapters/litellm/litellm_retry.py` |
| `adapters/smolagents/models/request_headers.py`、`adapters/litellm/request_headers.py` | 删除两级转发；模型装配和测试直接导入 `configuration/model_request_headers.py` |
| `adapters/smolagents/tools/shell/` 下的 `validator.py`、`security.py`、`path_validation.py`、`readonly_validation.py`、`shell_command_ast.py` | 删除 06 留下的中间别名；smol Shell 及仍使用的旧兼容入口直接指向 `runtime/tool_governance/shell/`，保持模块对象身份 |
| `tools/shell/subprocess_env.py` | 最后一个测试消费者改用 `runtime/subprocess_env.py`，生产调用者此前已迁移 |
| `tools/shell/output_reader.py`、`tools/shell/shell_command_ast.py`、`tools/file_ops/edit_file/utils.py` | 没有导入、包导出或动态注册消费者的内部叶子别名；实际实现分别留在 smol 和公共治理层 |
| `tools/queries/queries/` | 删除 58 个逐字相同的副本（56 个 SCM、2 个说明文件）；大纲和 LSP 只加载 `tools/queries/<provider>/`，保留其查询及来源说明 |

共删除 70 个文件，其中 12 个 Python 转发模块；未增加替代兼容层。已检查完整模块名、相对导入、包级导出、动态工具注册和资源加载路径。

## 保留的入口

`tools/file_ops`、`tools/search`、`tools/shell` 中仍被加载器、包导出、应用或测试使用的入口继续保留。`runtime.todo`、`tools.todo`、旧 prompt/error_recovery 接口以及用户显式模板路径仍属现有兼容承诺，不因实现迁移而整体删除。后续票据不得把已删除的内部别名作为新依赖。

未修改 Pi bridge/SDK、其他任务 worktree、未跟踪的参考仓库及运行数据。历史迁移清单和验收 JSON 保留为当时的证据，本记录说明其后的路径变化；14 仍等待原有前置任务。

## 验证

- 删除前逐文件比对重复查询与保留副本，内容一致。
- 删除后扫描受跟踪源码、测试、YAML、Shell 和 TOML，未发现已删除模块的剩余引用。
- 安装探针更新为 56 个实际查询文件，检查无重复目录，并通过大纲及 LSP 的真实资源解析入口读取 Python、TypeScript 查询。
- 对 718 个 Python 文件执行 AST 导入检查，未发现指向删除模块的绝对或相对导入。变更 Python 文件的 Ruff `F,E9` 检查及 `git diff --check` 通过。
- 从只包含当前受跟踪源码的干净目录构建 wheel；70 个删除路径均不在包内，实际查询文件为 56 个。隔离安装该 wheel（复用现有虚拟环境的第三方依赖，不启用源码 editable 导入）后，CLI、外部 Application 执行、动态工具身份和资源加载等 11 项安装检查通过。这不是 13 的 Pi-only 干净依赖验收。

完整回归命令：

```sh
.venv/bin/python -m pytest tests/ \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py \
  -q --tb=short
```

首轮结果为 **4379 passed、1 skipped、3 failed、3 warnings**，耗时 330.06 秒。原始失败保留，分别处理如下：

1. `test_legacy_governance_modules_are_declarative_reexports_only` 直接读取已删除的两个旧模型转发文件。该断言只约束过渡文件的形式，随转发模块一并删除；同文件中 LiteLLM 不依赖 smol 的实质边界测试保留并通过。
2. `test_real_campaign_release_sources_bind_harness_workflows_and_runtime` 在删除尚未登记到 Git index 时，读取到了已不存在的 tracked 路径，按原有规则返回空清单。登记删除后通过，未修改或放宽发布清单逻辑。
3. Pi 的 `duplicate_terminal` 故障注入用例遇到 `[Errno 1] Operation not permitted`，未返回预期协议错误。当前代码单项复跑通过；清理前 `07469855` 的全部 12 项 Pi 生命周期测试及随后 8 次该单项检查也通过，未复现这次系统权限错误。本次未修改 Pi 源码或放宽其断言，不能据此宣称已经修复该间歇性失败。

登记删除、清理过渡断言后，模型依赖方向、全部 Pi 生命周期和发布清单检查合计 **14 passed**（37.89 秒）。这是失败项及相关用例的复验结果，不冒充另一次完整回归全部通过。本次没有运行远程模型验收。

构建、安装、首轮回归、基线对照和复验日志保存在仓库外的 `AgentLoom-validation/migration-cleanup-20260920/`。

## 磁盘残留补充清理

维护者指出 `src/extensions` 仍出现在磁盘上。以 `e75e7a56` 为起点重新遍历项目工作目录后，确认上一轮只检查受跟踪文件，漏掉了 Git 忽略的迁移前 Python 字节码。这些源码已经删除，`__pycache__` 却让旧目录继续存在。

本轮删除 354 个没有对应 `.py` 源文件的字节码、22 个 Finder `.DS_Store` 文件，并从叶子向上移除 118 个空目录，合计清理约 6.7 MiB。每个文件删除前确认未被 Git 跟踪；每个目录仅在实际为空时调用 `rmdir`，不递归删除含内容的目录。

| 范围 | 已清理的主要目录 |
| --- | --- |
| 迁移前源码命名空间 | `src/extensions`、`src/lib`、`src/services`、`src/mcp`、`src/trace`、`src/workflows` |
| 已移除工具和空包 | `src/tools/code_editor`、`src/tools/git`、`src/runtime/memory` |
| 旧辅助目录 | 根目录 `hooks`、`scripts`、`cache`、`__pycache__`、`.scratch`；旧应用、测试和 Skill 的空子目录 |

唯一仍被 Git 跟踪的纯空包是 `src/runtime/memory/__init__.py`，也已删除；该包没有其他源码，也没有导入或动态注册消费者。仍有真实子模块的空 `__init__.py` 和应用验收所需的空配置文件继续保留。离线记忆验收脚本中的历史路径映射用于读取旧 Git 提交，不依赖这些磁盘目录，保持原有能力。

扫描排除了独立参考仓库、虚拟环境、已安装 Node 依赖及构建产物、运行数据和用户本地配置。完成后对相同范围重新遍历，空目录和无源码字节码均为 0。多数清理项原本被 Git 忽略，属于本机磁盘清理；Git 中记录空包删除和本次审计结果。

架构导入边界、工具懒加载、记忆模型导入边界和外部安装探针共 **10 passed**（6.81 秒）。本轮只删除空包和生成残留，没有重复运行模型与工具全量回归。文件级删除清单及补充测试日志保存于仓库外的 `AgentLoom-validation/migration-cleanup-20260920/residual-cleanup.json`、`residual-cleanup-tests.log`。

## 移除旧 Codex CLI 工具

以 `be866b16` 为起点，按维护者要求删除 `src/tools/codex/`。该实现仅将本地 `codex exec` 包为普通函数工具，未接入 Agent runtime 合同；仓库内只有专属 demo 和测试使用。同步删除 `applications/codex_exec_demo/`、`tests/tools_test/codex/` 及中英文 README 的示例入口，共删除 5 个受跟踪文件，并清理 4 个字节码缓存和空目录。

通用 YAML `fixed_args` 测试保留，改用现有 `sample_tool`，动态工具用例直接调用真实模块加载器。固定参数覆盖、LLM schema 隐藏参数和多别名能力不受影响。模型 header 自定义 profile 中的 Codex 示例与这个 CLI 工具无关，继续保留。

相关 YAML、catalog、应用定义和安装回归 **74 passed、2 warnings**（8.56 秒）；全仓活动源码/配置/测试已无旧模块及 demo 引用，fresh import 确认旧包不存在。干净源码构建的 wheel 不含旧 Codex 工具，Pi 和 smol runtime 仍在包内；Ruff `F,E9` 及 diff 检查通过。旧的 `agentloom.tools.codex.codex_tool` YAML 调用入口不再支持，未来基座接入按 [工具归属](tool-ownership.md) 中的 adapter 边界实现。本次没有运行外部 Codex CLI。

测试和构建日志保存在仓库外的 `AgentLoom-validation/migration-cleanup-20260920/codex-removal/`。
