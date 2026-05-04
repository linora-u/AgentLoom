# Workflow 架构审核报告：sample_complex_supervisor

> 验证样例（通用化）：展示多阶段 Supervisor + 多 Worker 的典型发现写法。

## 审核概要
- Application: sample_complex_supervisor
- 模式: Supervisor + 6 Worker
- Worker 数量: 6
- 自定义 Tool 数量: 2

---

## 发现 1：Worker 契约过于笼统
[证据]
- 多个 Worker 的 `agent_function_schema.inputs` 仅有 `query`。

[问题判断]
- Supervisor 难以稳定构造参数，跨阶段数据传递不透明。

[改进建议]
- 将 `query` 拆分为语义化字段（如 `target_path`、`context_text`、`focus_scope`）。

[置信度]
- 高

[推断]
- 否

---

## 发现 2：Prompt 中确定性逻辑过重
[证据]
- Worker prompt 要求“列举全部文件并按规则排序输出”。

[问题判断]
- 文件遍历与排序属于确定性操作，放在 LLM 中会增加漏检与 token 消耗。

[改进建议]
- 新增 `collect_and_sort_files` Tool；Agent 只消费结构化结果并做语义分析。

[置信度]
- 高

[推断]
- 否

---

## 发现 3：缺少并发与重试预算
[证据]
- workflow 无并发上限、超时或最大重试轮次说明。

[问题判断]
- 复杂任务可能出现执行时间不可控和成本失控。

[改进建议]
- 增加 `max_parallelism`、`timeout_seconds`、`max_retry_rounds` 的显式约束。

[置信度]
- 中

[推断]
- 是

