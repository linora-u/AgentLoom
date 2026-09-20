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
