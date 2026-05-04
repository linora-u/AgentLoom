# Workflow Architecture Review Report: sample_complex_supervisor

> Validation example (generalized): Demonstrates typical findings for multi-phase Supervisor + multiple Workers.

## Review Summary
- Application: sample_complex_supervisor
- Pattern: Supervisor + 6 Workers
- Worker Count: 6
- Custom Tool Count: 2

---

## Finding 1: Worker Contracts Are Too Vague
[Evidence]
- Multiple Workers' `agent_function_schema.inputs` only have `query`.

[Issue Assessment]
- Supervisor struggles to reliably construct parameters; cross-phase data passing is opaque.

[Improvement Recommendation]
- Split `query` into semantic fields (e.g., `target_path`, `context_text`, `focus_scope`).

[Confidence]
- High

[Inference]
- No

---

## Finding 2: Excessive Deterministic Logic in Prompts
[Evidence]
- Worker prompt requires "enumerate all files and sort output by rules".

[Issue Assessment]
- File traversal and sorting are deterministic operations; placing them in LLM increases missed detections and token consumption.

[Improvement Recommendation]
- Create a `collect_and_sort_files` Tool; Agent only consumes structured results for semantic analysis.

[Confidence]
- High

[Inference]
- No

---

## Finding 3: Missing Concurrency and Retry Budget
[Evidence]
- Workflow has no concurrency limit, timeout, or maximum retry round specifications.

[Issue Assessment]
- Complex tasks may result in uncontrolled execution time and cost overruns.

[Improvement Recommendation]
- Add explicit constraints for `max_parallelism`, `timeout_seconds`, and `max_retry_rounds`.

[Confidence]
- Medium

[Inference]
- Yes
