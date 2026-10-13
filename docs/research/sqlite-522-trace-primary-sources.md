# AgentLoom 2025-08-25 SQLite 522：Trace 与一手资料核验

状态：2026-10-13，只读研究；未修改产品代码、依赖、数据库或运行配置。

## 结论

现有 Trace **能把问题定位到一次真实的底层短读，但还不能定位到实施短读的文件和 actor**。

- 两次错误都来自 PID 71445，SQLite 3.53.1，扩展码为
  `522 / SQLITE_IOERR_SHORT_READ`，相隔 2.313 ms；调用栈都在读取
  `loom_durable_attempts` 时失败。原始记录见
  `.agentloom/validation/durable-news-full-2025-08-25-60056cef-v1/campaign/sqlite-diagnostics/71445-errors.jsonl`。
- 522 不是锁码。SQLite 官方把它定义为 VFS 请求读取 N 字节、实际没有取得 N
  字节，可能由文件被截短造成；SQLite 3.53.1 的 Unix VFS 在 `read/pread`
  返回非负但少于请求量时归零剩余缓冲并返回 522。[结果码定义][sqlite-522]
  [3.53.1 Unix VFS 源码][sqlite-unix-read]
- “同一进程有两个独立 SQLite 核心”是官方明确确认的高危结构，**可能**破坏
  同进程锁协调；但没有一手资料证明它必然或直接产生 522。把它闭合到本次
  522，还缺“某一核心缩短/替换 WAL，另一核心随后跨 EOF 读旧 frame”的现场证据。
- 普通 CPython `Popen(close_fds=True)` 在 child 中 raw-close 继承的 SQLite fd，
  **不会释放 parent 的 POSIX 进程锁，也不会关闭 parent 的 fd**。SQLite 官方
  甚至明确说 fork 后 child 关闭底层 fd 是安全的；危险的是 child 使用继承的
  SQLite connection 或调用 `sqlite3_close()`。因此，后续 Trace 中看到的 child
  pre-exec `close(fd)` 不是本次 522 的充分原因。

所以当前最窄、可检验的根因表述是：

> PID 71445 的 SQLite VFS 在读取数据库主文件或 WAL 时没有取得请求的全部字节。
> 是哪个文件、为什么发生短读，以及造成短读的 actor，原始 Trace 都没有记录。

## 本机机制复现

本轮另外使用 Python 3.14.8、内置 SQLite 3.53.1、系统 SQLite 3.51.0 和临时
WAL 数据库做了三组只操作临时目录的机制实验。它们没有读取或修改业务数据库。

第一组中，内置 SQLite 持有 `BEGIN IMMEDIATE` 时，系统 SQLite 直接尝试
`BEGIN IMMEDIATE`，正确得到 `SQLITE_BUSY`。这说明两套核心并存不会自动越过
正常的内核锁。

第二组中，内置 SQLite 持有未提交写事务，系统 SQLite 对同一数据库执行一次
打开、读取、关闭。此前得到 `SQLITE_BUSY` 的外部进程随后可以取得写锁并提交；
原内置连接也报告提交成功，但外部提交的行最终消失，`integrity_check` 仍为
`ok`。这直接复现了 SQLite 官方说明的同进程多核心锁协调失效机制。

第三组把该机制接到 WAL 文件生命周期：

1. 内置 SQLite 在 WAL 数据库中完成一次查询并保留写事务；
2. 同 PID 内的独立核心打开、读取、关闭同一数据库，或代码直接打开再关闭
   同一数据库的 `-shm` 文件；
3. 外部进程取得本不该取得的写锁，写入后执行
   `PRAGMA wal_checkpoint(TRUNCATE)`；
4. 原连接读取尚未加载的 WAL 页面，稳定得到
   `522 / SQLITE_IOERR_SHORT_READ`；
5. 原连接仍可报告提交成功，停止后的 `integrity_check` 仍为 `ok`。

对照实验显示，只打开并关闭 main 或 `-wal` 文件不会释放 WAL 写锁；打开并关闭
`-shm` 会让外部进程立即取得写锁。这与 WAL 锁字节位于共享内存文件的官方实现
一致。

该实验使“同 PID 内未被当前 SQLite 核心协调的 SHM close → 锁提前释放 → peer
checkpoint 截短 WAL → 原连接跨 EOF 读取 → 522”成为已经复现的因果机制。但是，
原 2025-08-25 Trace 没有记录故障窗口的 SHM close、调用者映像或失败 read
范围，因此仍不能把历史故障的具体 actor 定案为系统 SQLite、过期 fd
二次关闭或其他原生代码。

## 问题一：两个 SQLite 核心能否导致 WAL 短读

### 官方已经确认的部分

Unix 的 `fcntl(F_SETLK)` 锁属于进程，同进程不同 fd 设置的锁不会彼此冲突。
SQLite 因此不能只靠 POSIX 锁协调同进程连接，而是在每个 SQLite 核心内维护一份
按 `(st_dev, st_ino)` 索引的 `unixInodeInfo`、锁计数和延迟关闭队列。
[SQLite 3.53.1 `os_unix.c` 设计说明][sqlite-unix-locks]

SQLite 官方明确列出“同一应用链接多个 SQLite 副本”为数据库损坏模式：每个副本
有自己的全局打开文件表，一个副本不知道另一个副本的连接；一个副本的
`close()` 可能清除另一个连接依赖的 POSIX 锁。官方还记录过 Linux/macOS 商用品
因此发生低频损坏，修复方式是只链接一个 SQLite 副本。
[SQLite How To Corrupt 2.3][sqlite-multiple-copies]

SQLite 3.51.0 增加了 WAL/多进程场景下抵抗 rogue `close()` 破坏锁的防御；
3.53.1 已包含它。但官方明确说这不是完整修复，连接存活时仍不得绕过 SQLite
raw-close 同一数据库文件的 fd。[3.51.0 发布记录][sqlite-351-release]
[官方限制说明][sqlite-close-locks]

SQLite 官方 3.53.1 至 3.53.4 分支时间线还出现过只读 WAL 页大小、共享内存映射等
修复，但没有条目把普通可写 WAL 数据库的 522 归因为双核心或 Popen fd 清理；
因此“升级 SQLite”也不是由当前证据建立的修复。[官方分支时间线][sqlite-353-timeline]

WAL 模式的 WRITE、CHECKPOINT、RECOVER 和 READ 锁位于 `-shm` 锁字节；read-mark
保证活跃 reader 仍需的 WAL frame 不会被 checkpoint/reset 覆盖。正常实现中，
TRUNCATE checkpoint 要先取得全部 reader lock，然后才更新 WAL header 并把 WAL
截为 0。[WAL 并发说明][sqlite-wal-concurrency]
[WAL 锁与 read-mark 源码][sqlite-wal-locks]
[TRUNCATE checkpoint 源码][sqlite-wal-truncate]

### 能闭合到 522 的机制

以下链条与官方锁语义和源码一致，但**尚未由原 Trace 证实**：

1. 同 PID 内两个 SQLite 核心分别维护进程内 WAL 锁状态，彼此不可见。
2. 因 POSIX 进程锁不会阻挡同 PID 的另一个核心，双方可能都认为自己取得了冲突锁，
   或一方的 close/unlock 清掉另一方依赖的锁。
3. 一方在另一方仍按旧 read-mark/frame 读取时 reset、truncate、unlink 或替换 WAL。
4. 另一方按旧 frame offset 调用 `sqlite3OsRead()`，`pread()` 在 EOF 前返回不足请求量。
5. Unix VFS 返回 522。

第 4 步是 SQLite 3.53.1 的实际读取路径：`sqlite3WalReadFrame()` 根据 wal-index
给出的 frame 号计算 WAL offset，再从 `pWalFd` 读取一页。[WAL frame 读取源码][sqlite-wal-frame-read]

SQLite 3.53.1 自己也承认一种窄场景：当 shared-memory 不可靠时，
`walIndexRecover()` 可能因为并发 writer 在其读取过程中截短 WAL 而得到
`SQLITE_IOERR_SHORT_READ`，随后 SQLite 会重试。[`walIndexReadHdr()` 源码][sqlite-wal-retry]
这证明“并发 WAL truncate → 522”在实现上成立，但不证明本次运行进入了该路径。

### 原 Trace 为什么不能证明双核心

- 故障 PID 71445 没有保存加载映像、SQLite 符号地址、`sqlite3_sourceid()` 或
  每个 connection 所属核心。
- 后来 `vmmap` 中同时出现 builtin SQLite 和 `/usr/lib/libsqlite3.dylib` 的对象是
  诊断 parent PID 48673，不是故障 PID 71445；不能把它外推到原故障。
- 原 Trace 没有记录失败 read 的文件、fd、device/inode、offset、请求长度、实际长度，
  也没有记录同一 inode 的 `ftruncate/unlink/rename`。
- 停止后的 WAL 为 0 字节，只是事后状态。SQLite 正常关闭最后一个连接时也会最终
  checkpoint 并删除 WAL/SHM；它不能证明故障瞬间发生了异常截短。
  [WAL 自动 checkpoint/清理说明][sqlite-wal-cleanup]
- native probe 首次加载比原 522 晚约 1160 秒，后续捕获的 truncate/unlink 属于恢复，
  不是原错误现场。

因此，双核心假设当前判断为：**机制可信，现场前提和因果链未证实**。

## 问题二：Popen/fork/exec 关闭继承 fd 会不会破坏 parent

### POSIX 和 SQLite 的边界

POSIX 规定：

- process-owned record locks 不由 fork child 继承；
- 某进程关闭该文件的任一 fd，只移除“该进程”拥有的 process-owned locks；
- OFD locks 只有在关联 open file description 的全部 fd 都关闭后才移除。

因此 child raw-close 自己继承的 fd，不会移除 parent 进程拥有的锁。
[POSIX `fcntl()`][posix-fcntl]

SQLite 官方禁止 child 使用 parent 创建的 SQLite connection，也禁止 child 对其调用
`sqlite3_close()`，因为 SQLite 级清理可能删除 parent 正在使用的内容；但同一段明确
写道：`It is safe to close the underlying file descriptor`。
[SQLite fork 边界][sqlite-fork]

### CPython Popen 实际做了什么

Python 3.14 文档说明 `close_fds=True` 时，child 在 exec 前关闭 0/1/2 之外未保留的
fd；`preexec_fn` 在多线程应用中不安全，主要风险是 child 在 exec 前死锁。
[Python 3.14 `Popen`][python-popen]

CPython 3.14 源码有两条实现路径：

- `posix_spawn` 路径添加 `POSIX_SPAWN_CLOSEFROM`；
- fork/exec 路径在 child 的 `child_exec()` 中调用 `_close_open_fds()`，然后
  `execve/execv`。这只是 raw fd close，没有调用 SQLite API。

[CPython 3.14 `subprocess.py`][cpython-subprocess]
[CPython 3.14 `_posixsubprocess.c`][cpython-posixsubprocess]

原校准中 child pre-exec close 确实关闭过 main/WAL/SHM 的继承 fd，但 parent 的真实
writer lock 一直有效，peer 只能在 parent commit 后取得锁；这与上述官方边界一致。

所以应从候选中排除：

> “普通 Popen child 因 `close_fds=True` 关闭继承的 SQLite fd，直接释放 parent
> 的锁并使 parent 读短。”

仍值得追踪的 fork 变体仅有：

- child 使用继承的 SQLite connection 或调用 `sqlite3_close()`；
- child/preexec/at-fork hook 对 DB/WAL/SHM 执行 write、truncate、unlink 或 rename；
- fd 被显式保留并跨 exec，exec 后程序又操作数据库文件；
- 实际 close 发生在 parent PID，而不是 child PID。

原 Trace 没有支持这些变体。

## 下一次 Trace 的最小判定合同

不要再用“看到了 child close”或“进程里可能有两个 libsqlite”作为结论。要证实
“双核心导致本次 522”，一次错误窗口至少要同时保留：

1. SQLite 异常：PID、native TID、connection ID、Attempt ID、事务阶段、
   `sqlite_errorcode`、开始/结束 wall-clock ns。
2. 核心身份：每个 connection 对应的 SQLite 映像/符号地址、
   `sqlite3_sourceid()`；证明同 PID 确有两套核心。
3. 文件身份：main、WAL、SHM 的绝对路径、device、inode，以及每次 fd 绑定。
4. 锁事件：WAL WRITE/CKPT/RECOVER/READ 锁字节的 acquire/release 结果，并标出
   发起它的 SQLite 核心。
5. 文件生命周期：同 inode 的 `ftruncate/truncate/unlink/rename/dup/close/pwrite`，
   包含 PID/TID、调用者映像和 before/after size。
6. 失败读取：文件身份、offset、requested、actual、before/after size、调用者；
   必须能看到 `actual < requested`，并与 522 紧邻。

判定规则：

- 若失败读取不是 WAL 跨 EOF，或错误窗口没有 WAL 缩短/替换，则“WAL truncate
  导致 522”这条具体链路被证伪。
- 若故障 PID 只有一套 SQLite 核心，则“双核心”前提被证伪。
- 若只有 child PID raw-close，而 parent fd/inode 和锁仍在，则归类为正常 Popen
  清理，不归因为故障。
- 只有同时看到“两核心访问同一组 inode → 冲突锁均成功/锁被意外清除 →
  WAL 缩短或替换 → 旧 offset 短读 → 522”，才能关闭这条 RCA。

## 来源

所有外部结论只使用 SQLite、CPython/Python 和 POSIX 官方资料。

[sqlite-522]: https://www.sqlite.org/rescode.html#ioerr_short_read
[sqlite-unix-read]: https://github.com/sqlite/sqlite/blob/version-3.53.1/src/os_unix.c#L3456-L3578
[sqlite-unix-locks]: https://github.com/sqlite/sqlite/blob/version-3.53.1/src/os_unix.c#L1194-L1255
[sqlite-multiple-copies]: https://www.sqlite.org/howtocorrupt.html#multiple_copies_of_sqlite_linked_into_the_same_application
[sqlite-close-locks]: https://www.sqlite.org/howtocorrupt.html#_posix_advisory_locks_canceled_by_a_separate_thread_doing_close_
[sqlite-351-release]: https://www.sqlite.org/releaselog/3_51_0.html
[sqlite-353-timeline]: https://www.sqlite.org/src/timeline?from=version-3.53.1&to=version-3.53.4&to2=branch-3.53&y=ci
[sqlite-wal-concurrency]: https://www.sqlite.org/wal.html#concurrency
[sqlite-wal-locks]: https://github.com/sqlite/sqlite/blob/version-3.53.1/src/wal.c#L280-L400
[sqlite-wal-truncate]: https://github.com/sqlite/sqlite/blob/version-3.53.1/src/wal.c#L2347-L2380
[sqlite-wal-frame-read]: https://github.com/sqlite/sqlite/blob/version-3.53.1/src/wal.c#L3647-L3667
[sqlite-wal-retry]: https://github.com/sqlite/sqlite/blob/version-3.53.1/src/wal.c#L2637-L2752
[sqlite-wal-cleanup]: https://www.sqlite.org/wal.html#ckpt
[sqlite-fork]: https://www.sqlite.org/howtocorrupt.html#_carrying_an_open_database_connection_across_a_fork_
[posix-fcntl]: https://pubs.opengroup.org/onlinepubs/9799919799/functions/fcntl.html
[python-popen]: https://docs.python.org/3.14/library/subprocess.html#popen-constructor
[cpython-subprocess]: https://github.com/python/cpython/blob/8e6e75d9102e39bed2a2b279203a396741180f12/Lib/subprocess.py#L1786-L1925
[cpython-posixsubprocess]: https://github.com/python/cpython/blob/8e6e75d9102e39bed2a2b279203a396741180f12/Modules/_posixsubprocess.c#L634-L835
