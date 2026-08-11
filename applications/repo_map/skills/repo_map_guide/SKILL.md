---
name: repo_map_guide
description: Guide for generating and using AI-readable code maps
---

# Repo Map 生成指南

## 概述

Repo Map 是一个代码地图生成系统，参考 Aider 的 RepoMap 实现，
将任意代码项目扫描后生成 AI 可读的 Markdown 文件目录结构。

## 输出结构

```
<output_dir>/
  repo_map/
    index.md                   # 项目总览 + PageRank top-20 关键模块
    dependencies.md            # 跨文件依赖关系
    <dir>/
      index.md                 # 该目录所有文件的定义+引用摘要
      analysis.md              # LLM 生成的架构分析（step3）
  data/
    tags.json                  # 所有文件的代码 tags（原始数据）
    ranked.json                # PageRank 排序结果
    scan_meta.json             # 扫描元信息
    analysis_progress.json     # step3 执行进度（断点续传）
```

## index.md 格式说明

每个目录的 `index.md` 包含：

```markdown
# src/core/

- 文件数: 12
- 总定义数: 78

## 文件列表（按重要性排序）

### engine.c ★★★★★
*src/core/engine.c*

- `engine_init` (line 28)
- `engine_run` (line 45)
- `process_event` (line 78)

*被引用于*: `main.c`, `handler.c` ... +3 more
```

## PageRank 重要性说明

⭐ 星级由 PageRank 分数决定：
- ★★★★★ — 被最多文件引用的核心模块
- ★★★★  — 重要模块
- ★★★   — 中等重要
- ★★    — 较少被引用
- ★     — 边缘模块

## analysis_progress.json 格式

```json
{
  "src/core": {
    "status": "completed",    // pending | in_progress | completed | failed
    "md_file": "/abs/path/repo_map/src/core/index.md",
    "output": "/abs/path/repo_map/src/core/analysis.md",
    "rank": 1,
    "file_count": 12
  }
}
```

## 断点续传机制

step3 通过 `analysis_progress.json` 支持断点续传：
1. Supervisor 读取 progress 文件
2. 跳过 `status == "completed"` 的目录
3. 每次迭代前设置 `status = "in_progress"`（立即写回）
4. 完成后设置 `status = "completed"`（立即写回）
5. 中断重启后，`in_progress` 状态自动重置为 `pending`

## 使用方式

```bash
# 基本用法
.venv/bin/python applications/repo_map/repo_map_app.py /path/to/project

# 完整参数
.venv/bin/python applications/repo_map/repo_map_app.py /path/to/project \
  --output_dir /tmp/mymap \
  --exclude_dirs vendor \
  --exclude_dirs third_party \
  --exclude_dirs build
```

## 支持的语言

通过 tree-sitter 支持 30+ 语言，包括：
C, C++, Python, JavaScript, TypeScript, Java, Go, Rust, Ruby,
Swift, Kotlin, Dart, Elixir, Elm, Haskell, OCaml, Scala, Zig 等

## 注意事项

- [pitfall] 大型项目（>1000 文件）扫描较慢，属正常现象
- [pitfall] 二进制文件和 .min.js 等自动跳过
- [decision] step3 每次只读一个目录的 index.md，避免 context 超限
- [fact] analysis_progress.json 支持中断恢复，不用从头重跑
