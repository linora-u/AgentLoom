# Self-learning v6

AgentLoom 将可搜索的执行历史与 Curated Memory 分开：History 记录发生过什么；Memory 只保存经过评审、可注入后续运行的事实和经验。数据库是权威状态，Markdown review artifacts 只是人工评审面，不是第二份状态库。

## 配置

```yaml
self_learning:
  enabled: true
  events_retention_days: 90
  memory:
    prompt_max_chars: 12000
    max_item_chars: 4000
    scope_budgets:
      project: 8000
      application: 6000
  review:
    enabled: true
    application:
      review_model: summary
      trigger: {mode: batch, min_completed_runs: 5}
      approval: {fact: auto, experience: manual}
    project:
      review_model: summary
      trigger: {mode: batch, min_candidates: 5}
      approval: {fact: manual, experience: manual}
    artifacts:
      markdown: true
      review_auto_applied: true
```

`events_retention_days` 当前是兼容性保留值。History 清理须显式执行 `loom sessions prune --retention-days N`，并使用 CLI 传入的值。

`trigger.mode` 可选 `manual`、`after_run`、`batch`。`manual` 只通过 CLI 触发；`after_run` 在成功的 root run 且存在评审上下文时触发；`batch` 等到已完成 run 或 candidate 达到阈值。

`fact` 与 `experience` 分别配置审批策略。`auto` 仍须通过代码证据门和容量检查，成功后进入 `active_unreviewed`，人类仍可 `acknowledge` 或 `revoke`；`manual` 保持 `pending_pre_review`，直到 scoped INBOX 被应用。

在 `self_learning.review` 内，Application 或 Agent overlay 只能覆盖 `application`；`review.enabled`、`review.project` 和 `review.artifacts` 属于项目根配置。其他 `self_learning` 字段仍按普通 overlay 契约合并。

## Scope 与 Candidate 契约

模型侧 `memory` 工具只有 `list` 和 `propose`：

- `list` 可以查看 Project 与当前 Application 的记忆。
- `propose` 只能为当前 Application 创建 candidate，不能指定 Project。
- fact payload 必须精确为 `{text}`。
- experience payload 必须精确为 `{trigger, symptom, action, verification}`。

Reviewer 是有界的结构化抽取器，不能选择 scope 或审批策略，不能 replace/remove/promote，也不能写文件或 Skill。Application review 读取该应用已完成的 root runs 和可信证据；Project review 只读取代码标记的 Project 证据，以及至少两个不同 Application 对同一 typed memory 的交叉印证，不读取原始 Application transcript。

可搜索 History 不会被确定性 fallback 转成 Memory。使用 `loom sessions search/scroll/index/prune` 管理 History。

## 人工评审与 Project Promotion

默认 `review.artifacts.markdown: true` 时，评审文件位于：

```text
.agentloom/reviews/
├── applications/<application_id>/
│   ├── batches/<review_id>/{review.json,REPORT.md}
│   └── INBOX.md
├── project/
│   ├── batches/<review_id>/{review.json,REPORT.md}
│   └── INBOX.md
└── INDEX.md
```

Batch 文件是不可变证据，只编辑 scoped `INBOX.md`，再应用。`markdown: false` 时使用不可变 `review.json`、可编辑 `INBOX.json` 和 `INDEX.json`，且不生成 `REPORT.md`。决策包括 `approve`、`acknowledge`、`reject`、`revoke`、`correct`、`promote_project`；candidate id 与 revision 校验会拒绝过期编辑。

Project promotion 必须由人类发起。`promote_project` 只能从 Application candidate 开始，并通过 activation evidence 检查。成功后 Project 项成为 `active_confirmed`，Application 项变为 `shadowed`；同 key 的冲突 Project payload 会被拒绝。

```bash
uv run loom learn review --application <id>
uv run loom learn review --project
uv run loom learn review --all-unreviewed --dry-run
uv run loom reviews status --application <id>
uv run loom reviews apply --application <id>
uv run loom reviews rollback <review_id>
```

`loom memory list/add/replace/remove/pending/stats/export` 是管理员直接维护 active memory 的接口，与模型的 proposal-only 工具不同。不存在 `loom memory approve` 或 `loom memory reject`。

## 从 v5 迁移

校验器会拒绝 `self_learning.memory.review_model` 和 `write_approval`。将模型选择迁移到 `review.application.review_model` 与 `review.project.review_model`，将全局写入开关改成各 scope 的 `approval.fact` 与 `approval.experience`；待审批内容通过 scoped INBOX 和 `loom reviews apply` 处理。
