# SQLite 3.51.1 的并发 WAL 关闭死锁

2026-10-14，NewsAgent 全量测试在关闭业务 SQLite 连接时挂起。本轮已通过仅使用 Python 标准库的临时数据库程序，复现相同的原生等待栈，并与 SQLite 官方修复核对。未修改 Python 环境、依赖或业务数据库。

原因是 SQLite 3.51.1 Unix VFS 的锁顺序反转。`unixLock()` 先取得文件的 `pLockMutex`，随后通过 `unixIsSharingShmNode()` 取得全局 `unixBigLock`；另一个线程的 `unixClose()` 先取得全局锁，再等待同一个文件锁。两条路径相遇后互相等待。官方 SQLite 3.51.2 已修复这个问题。

本次死锁与历史 `522 / SQLITE_IOERR_SHORT_READ` 分开处理。历史现场使用 SQLite 3.53.1，且未记录造成短读的文件操作；这次关闭死锁的诊断不能代替 [历史 522 研究](sqlite-522-trace-primary-sources.md) 要求的因果证据。

## 现场与最小程序出现相同等待栈

NewsAgent 全量测试 PID 16962 的 sample 保存为 `news-suite-hang-sample.txt`。主线程停在：

```text
pysqlite_connection_close
→ sqlite3WalClose
→ unixLock +604
→ pthread mutex wait
```

同一 sample 的 `Thread_33579027` 是另一个 Python 线程，停在：

```text
pysqlite_connection_close
→ sqlite3PagerClose
→ unixClose +84
→ pthread mutex wait
```

现场还有等待中的 Arrow 线程，但死锁链中确实有两个 Python 连接关闭线程。独立运行 14 项 controller 测试在 10.94 秒内通过，说明全量测试中的挂起不能由这组测试的单次成功排除。

最小程序只建立一个临时 WAL 数据库。两个线程各自打开连接、读取同一张表，经过 Barrier 后同时 `close()`；每轮结束再经过一次 Barrier。程序不导入 AgentLoom、DBOS、Arrow 或 MCP，也不 fork，不混用 SQLite 核心。

可复现脚本及输出保存在本机任务证据目录 `~/.codex/tmp/agentloom-managed-execution-20261014/`，脚本是 `sqlite-close-stress.py`。选定已有解释器后运行：

```sh
"$TASK_DIAG_PYTHON" "$TASK_DIAG_DIR/sqlite-close-stress.py" 1000 read wal
```

脚本只操作 `TemporaryDirectory`，包含 2 个线程及上述两个 Barrier。外部诊断父进程在 7 秒仍未退出时采样，再终止自己的隔离子进程；未终止业务进程。判定依据是两个线程持续停在 `close()`，不是只把执行时间稍长算作死锁。

| 已运行对照 | 结果 |
| --- | --- |
| Conda Python 3.12.13／SQLite 3.51.1，WAL，并发关闭，3,000 轮 | 20 秒仍未退出；两个线程停在 `close()` |
| 同环境，WAL，并发关闭，100 轮，重复 3 次 | 1 次死锁；原生 sample 与全量现场的 `unixLock +604`／`unixClose +84` 相同 |
| 同环境，WAL，并发关闭，1,000 轮，重复 3 次 | 2 次死锁，另 1 次完成；两个失败运行各保存了 native sample |
| 同环境，DELETE，并发关闭，1,000 轮 | 完成，0.20 秒 |
| 同环境，WAL，只串行关闭，1,000 轮 | 完成，1.66 秒 |
| 同环境，WAL，打开连接后不读取表，1,000 轮 | 完成，0.13 秒 |
| 既有 Python 3.14.8／SQLite 3.53.1，WAL，并发关闭，1,000 轮 | 完成，4.17 秒 |
| 同一既有 3.14 环境，WAL，并发关闭，3,000 轮 | 完成，8.60 秒 |

这是有竞争时序的故障，短循环不会每次触发。对照验证了 WAL 读取和同时关闭是本轮触发条件；单核心最小程序已足以复现，不需要把双核心或应用并发框架当作必要前提。

## 已安装库的反汇编与上游锁顺序一致

挂起环境的 `_sqlite3` 动态链接 Conda 的 `libsqlite3.3.51.1.dylib`。`sqlite_source_id()` 为：

```text
2025-11-28 17:28:25 281fc0e9afc38674b9b0991943b9e9d1e64c6cbdb133d35f6f5c87ff6af38a88
```

通过 LLDB 对该库文件离线反汇编，未执行或改写库。结果保存在 `sqlite3511-lock-disassembly.txt`：

- `unixLock +84` 取得 `pInode->pLockMutex`；随后 `+600` 调用全局 mutex enter，返回地址为现场的 `+604`。
- `unixClose +64` 取得 `unixBigLock`；随后 `+80` 调用 inode mutex enter，返回地址为现场的 `+84`。

[3.51.1 的 Unix VFS 源码](https://github.com/sqlite/sqlite/blob/version-3.51.1/src/os_unix.c) 对应 `unixLock()`、`unixClose()` 和 `unixIsSharingShmNode()`。同一文件的互斥锁说明明确要求：同时需要两把锁时，应先取得 `unixBigLock`，再取得 `pLockMutex`。该版本的 `unixIsSharingShmNode()` 在已经持有 inode 锁的路径中再次调用 `unixEnterMutex()`，违反了这个顺序。

## 官方 3.51.2 修复删掉了反向取锁

[SQLite 3.51.2 发布说明](https://www.sqlite.org/releaselog/3_51_2.html) 明确写道：

> Fix an obscure deadlock in the new broken-posix-lock detection logic.

对比 [3.51.2 的 `os_unix.c`](https://github.com/sqlite/sqlite/blob/version-3.51.2/src/os_unix.c)，该修复移除了 `unixIsSharingShmNode()` 中的 `unixEnterMutex()`／`unixLeaveMutex()`，同时调整引用计数检查；已核对的 [3.53.1 源码](https://github.com/sqlite/sqlite/blob/version-3.53.1/src/os_unix.c) 也保留了这个修复。实际库反汇编、两个独立现场和官方修改共同支持此次死锁的根因判断。

[官方 3.51.0 源码](https://github.com/sqlite/sqlite/blob/version-3.51.0/src/os_unix.c) 也包含同一取锁顺序，发布说明将该检测逻辑的引入归于 3.51.0。本轮没有运行安装在其他位置的 3.51.0 二进制，也未核验供应商是否回移修复；不能据版本字符串判断所有 3.51.0 构建的实际行为。

## 使用已验证的既有环境，保留并发合同

现有 canonical 环境已经是 Python 3.14.8／SQLite 3.53.1。上述最小程序在该环境通过，可以用它加载 integration 源码继续正式验收，保留两个原有环境，不需要安装或升级依赖。实际验收仍须记录解释器、SQLite 核心、源码身份和结果；这个局部对照不代表完整 NewsAgent 或平台验收已完成。

对已经证明不兼容的 3.51.1 Unix SQLite 环境，应在持久任务接纳前明确报错，避免把任务放入一个可能无声挂起的执行环境。此限制针对已确认的 SQLite 核心行为，不应改成禁止 Python 3.12。

串行关闭在本轮只是诊断对照。降低并发、加应用全局锁、延长 busy timeout 或增加强制并发上限，都不能修复 SQLite 内部的相反取锁顺序，也不能覆盖 DBOS 与其他库产生的连接路径。本次不增加这类绕过代码。

关键证据文件：`sqlite-close-differential-matrix.json`、`conda-close-100-1-sample.txt`、`close-matrix-wal-concurrent-0-sample.txt`、`close-matrix-wal-concurrent-1-sample.txt`、`sqlite3511-lock-disassembly.txt`。它们与原始环境对比结果保留在上述任务证据目录。
