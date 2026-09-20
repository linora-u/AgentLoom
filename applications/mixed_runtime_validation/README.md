# 混合基座 Application 验证

这组 YAML 展示同一个 Worker 业务契约如何连接两个真实基座：

- `workflows/smol_to_pi.yaml`：smol Supervisor → Pi Worker，Worker 使用官方 `read`。
- `workflows/pi_to_smol.yaml`：Pi Supervisor → smol Worker，Worker 使用自研 `read_file`。

两条 workflow 共用 `inspect_note(query)` 契约和 `fixtures/note.txt`。不使用写入/Shell 或 checkpoint/resume；Pi SDK 按项目锁定版本安装：

```sh
uv run --locked loom install-runtime pi
```

确定性验证只替换 HTTP 模型服务，两个 runtime、基础工具、公共 Hook、Goal、记忆库与审核门禁均真实执行：

```sh
uv run --locked python -m pytest tests/application_test/test_mixed_runtime_application.py tests/application_test/test_mixed_runtime_memory.py -q
```

真实 provider 验证读取本地 `config/llm.yaml` 的 profile，在新的私有目录内创建 Application、独立 runtime 与记忆库：

```sh
uv run --locked python tests/acceptance/mixed_runtime_validation.py \
  --output /absolute/path/to/new-evidence-directory \
  --profiles powerful responses_powerful
```

每个 profile 有 9 个场景：双向读取、并发及重复 Worker、Goal、跨 Worker ContextRef，以及 smol→Pi 认可记忆交接。子进程有截止时间，失败保留原始尝试和日志，不复用用户运行库。

ContextRef 场景显式选用已有专业工具 `get_file_outline`；读取大纲后替换原文件，再从原 ContextRef 取回旧大纲。Pi 原生 `read` 的结构化 SDK 回执仍由 native journal 留存，本例不把它描述为平台文本压缩。

`agent_tools/release_policy.py` 是本应用的业务工具，只读取固定的策略 fixture，校验枚举后声明一条有明确 Application scope 的可信事实。它不是新的基础文件工具，不向其他 Agent 默认注入。记忆验证经真实工具证据 → 审核候选 → 现有证据门禁 → 人工认可 API，随后删除原文件并让新 Pi Run 调用 memory/history。验证中的认可操作只作用于这次创建的隔离库；不会向 DB 直接写入认可记忆。
