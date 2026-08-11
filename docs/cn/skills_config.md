# Skills

AgentLoom Skill 是从约定目录发现的说明包：

```text
skills/<skill-name>/SKILL.md
applications/<application>/skills/<skill-name>/SKILL.md
```

Application 定义可以增加本地发现目录。`config/system.yaml` 中的路径相对项目根目录，
Application 或 Agent 配置中的路径相对 Application 根目录：

```yaml
skills:
  paths:
    - shared/skills
```

`paths` 是唯一的 Skill 配置字段。它不选择加载模式，也不授予执行权限。

## 运行时语义

Skill 的模型上下文加载始终按需进行：

1. Agent 启动时发现并解析 `SKILL.md` 包。
2. system prompt 只获得允许使用的 Skill 的 `name` 和 `description`。
3. 任务匹配时，模型调用 `skill(name)`。
4. 工具结果只把被选中的说明、基础目录和抽样文件列表加入对话。

Agent 没有 `skill` 工具时，catalogue 也不会显示。系统没有 eager 模式。
激活 Skill 不会授予文件、Shell、脚本或网络权限；Agent 的常规工具和权限仍是唯一依据。
读取包内资源或执行命令时，使用这些常规工具。

## `SKILL.md` 契约

必填 frontmatter：

```yaml
---
name: test-driven-development
description: Use when implementing behavior with tests.
---
```

支持的可选 frontmatter 与 OpenCode 包格式一致：

```yaml
license: MIT
compatibility: Requires git.
metadata:
  owner: platform
```

未知字段会被忽略。`hooks` 与 `enable-hooks` 会报错，因为 Hook 是独立的执行授权边界；
请通过 [`hooks`](hooks.md) 配置。

名称必须是最长 64 字符的小写 kebab-case；描述必须非空且最长 1024 字符。
非法 YAML、缺少必填字段、同一层级内重名都会报错。同名时 Agent 定义覆盖 Application，
Application 覆盖项目定义。

名为 `generated` 的目录不会进入运行时发现，因为自学习提案在显式晋升前必须保持未激活状态。
