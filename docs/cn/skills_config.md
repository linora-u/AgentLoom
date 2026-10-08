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

共享定义预检在分配 Run 前，发现并解析 Supervisor 和所有引用 Worker 的 `SKILL.md`，
按 Agent > Application > 项目规则解析同名技能。`skills.paths` 增加的目录使用同一套规则。

Pi Agent 将预检后的技能名称、描述和入口路径注册到 Pi 原生 Skills 系统，不扫描
用户级或项目 `.pi/skills`。选择原生 `read` 或 `bash` 工具时，Pi 在 system prompt 的
`<available_skills>` 中提供名称、描述和位置；模型按任务需要读取 `SKILL.md`，
再按技能目录解析相对参考文件路径。例如：

```yaml
agent_runtime: pi
skills:
  paths:
    - skills/news-catalyst
tools:
  - name: read
toolsets: []
```

技能注册不代表正文已经读取。Pi 的读取通过常规工具执行，可在工具记录中检查。
注册后的名称、描述和路径使用定义快照，原生 `read` 按调用时的文件内容读取正文与参考文件。

其他运行时继续使用平台 `skill(name)` 工具：system prompt 只提供名称和描述，
激活后将选中技能的已解析正文、基础目录和抽样文件列表加入对话。
Pi 仍兼容显式选择的平台 `skill` 工具；使用原生技能读取工具时不重复注入平台目录摘要。
没有 `read`/`bash` 或平台 `skill` 工具时，技能目录不会显示。系统没有 eager 模式。
技能不额外授予文件、Shell、脚本或网络权限，沿用 Agent 已有工具和权限配置。

Studio 与执行使用同一次静态检查的结果。非法 frontmatter、名称及同层重名会在
Run 分配前拒绝。检查只读取 Skill 数据，不创建模型、加载工具实现、连接 MCP 或执行 Hook。
平台激活使用准备好的正文；新的检查看到磁盘编辑，已有调用继续使用原正文。
激活时仍从当前目录采样资源文件位置，不冻结所有资源文件。

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
