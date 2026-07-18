# Skills 配置

AgentLoom Skill 使用 Claude Code 风格包结构：

```text
skills/<skill-name>/SKILL.md
skills/<skill-name>/references/
skills/<skill-name>/scripts/
skills/<skill-name>/assets/
```

`SKILL.md` 忽略大小写匹配（`SKILL.md` 或 `skill.md`）。框架不会加载散落的 Markdown，也不会加载 `skills.md`。

## SKILL.md Frontmatter

必填字段：

```yaml
---
name: test-driven-development
description: Test-driven development workflow.
---
```

支持的可选字段：

```yaml
allowed-tools: Bash, Read, Edit
argument-hint: "<task>"
arguments: [task]
when_to_use: Use when implementing or fixing behavior with tests.
model: powerful
context: fork
agent: reviewer
effort: high
shell: bash
```

未知 frontmatter 字段会被静默忽略。`hooks` 会明确报迁移错误，因为 Hook 使用独立的顶层 [`hooks`](hooks.md) 配置。旧字段 `when-to-use`、`argument-names`、`requires`、`disable-model-invocation`、`user-invocable` 不会被映射到新字段。

非法 YAML、缺 `name`、缺 `description`、非法 skill 名、重复 skill name 都会报错。

## Agent YAML

默认是按需加载：

```yaml
skills:
  load-mode: on-demand
  items:
    - skills/tdd
    - skills/debugging
```

全文加载用 `eager`，完整正文会进入 system prompt：

```yaml
skills:
  load-mode: eager
  items:
    - skills/strict-review
```

可以在单个条目上覆盖：

```yaml
skills:
  load-mode: on-demand
  items:
    - skills/tdd
    - path: skills/strict-review
      load-mode: eager
```

简写仍然可用：

```yaml
skills: skills/tdd

skills:
  - skills/tdd
  - path: skills/debugging
```

## 加载语义

- 配置了 skill，Agent 启动时就注册。
- `on-demand`：system prompt 只放轻量 catalogue，包含 `name`、`description`、`argument_hint`、`when_to_use`。
- `eager`：完整 skill 正文注入 `<eager_loaded_skills>`，不会重复出现在 catalogue。
- 不再支持 hidden、user-invocable、force-inject、invocation-control 状态。
- `list_skills(detail="full")` 会列出全部已配置 skill 及其运行策略。
- 对 eager skill 调用 `load_skill` 会返回去重提示，因为正文已经在上下文里。

## 资源文件

用 `read_skill_resource(skill, path, offset, limit)` 读取 skill 包内文件。路径必须留在 skill 目录内；目录逃逸会失败。

## 脚本执行

默认允许第三方脚本执行：

```python
run_skill_script("youtube-transcript", "npm install")
run_skill_script("youtube-transcript", "node transcript.js EBw7gsDPAYQ")
```

执行审计会记录命令、cwd、环境变量名、退出码、stdout/stderr 文件路径和 audit 目录。

用户需要限制时再显式配置：

```yaml
skills:
  load-mode: on-demand
  allow-scripts: false
  allow-network: false
  items:
    - skills/safe-review
```

`allow-scripts: false` 会阻断脚本执行。`allow-network: false` 会阻断常见网络命令，例如 `curl`、`wget`、`ssh`、`npm`、`pip`、`pnpm`、`yarn`。
