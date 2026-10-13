# smolagents Shell 接入受管理进程范围

2026-10-14。**两处 `start_new_session` 改成 scope-aware 仍不足以交付。正式 smol Shell 路径还依赖一个把“组不存在”误认为“进程退出”的 kill helper，后台 registry 也会先删除句柄、标记 killed，再尝试清理。** 最小接入应复用 AgentLoom 已有的具体 Popen／token 清理入口，保留原前台、自动转后台和显式后台规则；完整任务资源退出继续由外层 OS 所有者确认。[Shell spawn](../../src/runtimes/smolagents/tools/shell/process.py)、[显式后台 spawn](../../src/runtimes/smolagents/tools/shell/shell_tool.py)、[kill helper](../../src/runtimes/smolagents/tools/shell/tree_kill.py)

本轮只读分析，没有改代码、运行模型或新建调度器。以下本地缺口是固定源码判断，尚未运行新的故障用例。

## 实际版本与证据范围

研究 worktree 为 `/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof`，HEAD 为 `99f6e5d8ad4f2cd6e856a2b4acd991e9c28f30d1`。smol Shell 文件当时无未提交修改，且与主工作区逐字相同；共享 `execution/process.py` 和新增 process scope 是父任务正在验证的未提交候选。

实际 Python 环境安装 smolagents 1.26.0、psutil 7.0.0、MCP 1.26.0 和 AnyIO 4.11.0。smolagents 的 `agents.py`／`tools.py` 与官方 tag commit `12c1bc820eca50ace6f80a21d90426d41d74f845` 逐字一致；psutil `__init__.py` 与 release 7.0.0 commit `ea5b55605f857affa4e65fa27eb80f4f2bfebd63` 逐字一致。[smolagents 官方 Agent][S1]、[官方 Tool][S2]、[psutil 7.0.0][PS1]

本地关键源码 SHA-256：

| 文件 | 研究时 SHA-256 |
| --- | --- |
| `runtimes/smolagents/tools/shell/process.py` | `6a2341302bc1014ba6162be72a46d17136168655050984b20c2ca73634ff2a5c` |
| `shell_tool.py` | `2188e664c0bd8a66272d9ef135583829e8b9bbad9a394d701ed477cb144c2fc0` |
| `tree_kill.py` | `54e05066ce0a179adc10ed2e48993475bb022fc5690897eaa943474fc96ec19b` |
| `background_task.py` | `34e62e3487bec1562201310540c9d7bfe6e7a949d1bc5c2ca63e55e614f700b4` |

这些 Shell 实现属于 AgentLoom，不能把 smolagents 或 npm tree-kill 的项目名称当成它们已经提供的退出保证。

## 正式 Application 会执行这些本地工具

实际调用链为：

```text
loom run YAML / execute_app() / Application BoundWorker.invoke()
 → YamlConfiguredAgent 构建 RuntimeDefinition
 → SmolagentsRuntimeFactory
 → SmolagentsRuntimeAdapter.run()
 → ToolCallingAgentV2.process_tool_calls()
 → execute_tool_call_record() → ToolGateway.invoke()
 → AgentLoom shell_tool()
```

`tool_catalog.py` 将 shell_tool 与后台控制工具标为 runtime owner、smolagents provider。RuntimeFactory 使用 AgentLoom Tool Gateway；ToolCallingAgentV2 的正式 provider tool loop 直接调用 Gateway。兼容用的 Tool proxy 同样转回该 Gateway。上游 smolagents 提供 Agent／Tool 协议，没有替这些本地 Popen 自动安装进程组所有者。[catalog](../../src/runtimes/smolagents/tool_catalog.py)、[factory](../../src/runtimes/smolagents/runtime_factory.py)、[正式 tool loop](../../src/runtimes/smolagents/agents.py)、[proxy](../../src/runtimes/smolagents/tool_proxy.py)

多 Tool 调用使用 `copy_context().run` 提交到线程池；watchdog 和后台 monitor 使用 `copy_runtime_context()`。该函数返回 `contextvars.copy_context()`，因此受管理 scope 可以沿这些现有路径传递，仍需在真实 Runtime 中验证，不应依赖裸 Thread 默认继承。[Agent 并行调用](../../src/runtimes/smolagents/agents.py)、[context 复制](../../src/execution/context.py)

## 前台和后台各有一条正式 spawn 路径

| 触发 | 当前实际行为 | 接入必须保留的规则 |
| --- | --- | --- |
| 普通 shell_tool | ShellProcess `_exec_subprocess()` 使用 Popen，stdin 为 DEVNULL，stdout／stderr 写同一个 artifact fd，固定另建 session | CWD／profile／快照、输出处理、stall 检测和原返回码 |
| 前台超时，后台和 auto_background 开启 | 将同一个活 Popen 与输出 artifact 转给 BackgroundTaskRegistry，返回后台 task_id | 超时 promotion；不能改成一律 kill |
| 前台超时且后台关闭，或检测到交互 stall | graceful_kill，尝试 `proc.wait(timeout=5)`，超时被忽略，仍构造 timeout／interrupted 结果 | 原错误分类；新增清理失败应可见 |
| `run_in_background=True` 或单个尾部 `&` | `_run_in_background()` 直接 Popen，再注册后台任务与 watchdog，固定另建 session | 显式后台启动、task_id、原 output inode 与后台控制工具 |
| SizeWatchdog 检测输出超限 | 仅保存 PID，调用 tree_kill(pid, SIGKILL) | 输出限额；kill 必须指向原具体句柄 |

两处固定 spawn 位于研究时 `process.py:580` 和 `shell_tool.py:311`。前台被 promotion 后不会重新 spawn。替换成只返回 exit code 的 `run_process_to_fds()` 会失去活 Popen 的转交能力，改变现有业务行为，因此不适合作为这条路径的直接替代。[前台完整实现](../../src/runtimes/smolagents/tools/shell/process.py)、[显式后台](../../src/runtimes/smolagents/tools/shell/shell_tool.py)

## kill helper 在同组模式下有确定的错误分支

`tree_kill(pid)` 先 `os.killpg(pid, sig)`；捕获 ProcessLookupError 后直接返回 True，未执行后续单 PID kill。受管理 Shell 子进程继承 Application PGID 时，它的 PID 通常不等于 PGID。此时 killpg(child_pid) 可以返回 ESRCH，而 child PID 仍存在。POSIX 对 ESRCH 的定义是目标进程或目标进程组不存在，不能从“指定组不存在”推出“相同数值的 PID 不存在”。[当前 helper](../../src/runtimes/smolagents/tools/shell/tree_kill.py)、[POSIX kill][POSIX1]

`graceful_kill` 又忽略 tree_kill 的返回值，TERM 后只检查 PID 存在，升级 KILL 后无条件返回 True。它没有最后 wait，也没有检查权限失败或后代存活。将该值解释成“terminated or already gone”超出了实际证据。

两个误用都需要去掉：不能把 child PID 假设为 PGID，也不能把信号发送或 bool 返回当成退出。反向改成 `getpgid(child_pid)` 后 killpg 同样危险，那会把 Application 与其他工具一起作为目标。[POSIX getpgid][POSIX2]、[此前 scope 研究](pi-mcp-managed-process-scope-20261014.md)

## registry 与 Application 清理仍有句柄缺口

ShellProcessRegistry 注册的 resource callback 调用 ShellProcess.cleanup，但该 cleanup 只清 session／snapshot，没有保存或关闭 `_exec_subprocess()` 的活前台 Popen。前台 finally 停 watchdog、关输出 fd、删 artifact；也没有统一处理控制异常后的 native process 清理。因此，仅登记了 ShellProcess 对象，不能证明前台进程已经由 Application resource scope 拥有。[session 注册与 cleanup](../../src/runtimes/smolagents/tools/shell/process.py)

BackgroundTaskRegistry 确实按 RuntimeKey 保存 Popen，并注册 `close_task` resource callback。但是：

- `_terminate_keys()` 先从 registry pop，先标 killed，再尝试 graceful_kill；清理异常只写 warning，未等待具体进程退出。
- 当 root Popen 已退出，`process.poll()` 会跳过 kill，残留后代不在该判断内。
- `kill_task()` 等 monitor 5 秒后，可强制写入 killed／exit_code=-9，即使没有新的退出证据。
- monitor 的 completed／failed 来自 root poll；孙进程仍持有输出 fd 时，这个状态不表示完整资源退出。

这些都是源码事实。后台 task 状态仍可用于业务展示，但不能承担外层队列容量的物理退出凭据。[后台 registry](../../src/runtimes/smolagents/tools/shell/background_task.py)

Application 的 invocation 收尾会先关闭 Runtime，再 `close_instance_resources()`；根 Run 收尾还会调用 `close_run_resources()`。通用 resource 层在 callback 抛错时保留原 handle，但它不能修复一个内部已经 pop 后只写 warning 的 callback：第二次调用可能找不到原 task，只返回 0。[invocation 收尾](../../src/app/invocation.py)、[Run 收尾](../../src/app/lifecycle.py)、[resource handle](../../src/execution/resources.py)

## 最小复用应围绕具体句柄，而不新增 registry 调度

父任务当前 [scope-aware process 候选](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/src/execution/process.py) 已公开导出 `terminate_process_tree(Popen, token)`。它知道同组工具不能 kill Application 父组，使用具体 descendant 句柄与 token，并检查 `psutil.wait_procs()` 的 alive 返回值；退出未确认时会抛错。它仍是正在验证的候选，不能当成已验收的通用任意后代容器。

smol 应复用这一个具体进程清理入口：

- 两条 spawn 使用现有 `child_starts_new_session()` 选择普通或 managed 模式；同一个具体 Popen 与 token 从启动贯穿前台、promotion、registry、watchdog 和关闭。
- foreground resource closer 应握住具体 Popen，不只握 ShellProcess session。promotion 转移其生命周期，不能让前台 finally 把仍在后台运行的句柄取消。
- tool timeout、stall、显式 kill、输出 watchdog 和 Run cleanup 调用同一 closer；不要各自用裸 PID 另猜进程组。
- 关闭失败要保留原句柄并向 Application 报告。可以让 callback 捕获独立具体句柄，或在确认关闭前保留 registry 条目；不能 pop 后令后续 cleanup 永远失去目标。

这是少量资源生命周期接线。现有 BackgroundTaskRegistry 继续负责运行中的任务状态／输出展示，不加入 claim、lease、持久 slot 或任务重试。现有前台 promotion 和不同 Run 的隔离范围继续保留。

若共享入口仍有不适合该路径的输出或 grace 合同，psutil 7.0.0 提供成熟公开组合：取得 `Process` 对象及创建时间、`children(recursive=True)`、按原对象发信号，再 `wait_procs()` 检查 alive。官方文档本身就用此方式示例；必须保留失败和未知结果。[psutil 进程树示例][PS2]

psutil 同时明确提醒：中间祖先进程消失，后代会从 children(recursive=True) 的结果中丢失。因而 Popen wait、树快照和 token scan 仍不能覆盖所有 reparent／清空 env／自主 setsid。完整任务退出由外层 OS scope 确认；同组的资源最终仍受该所有者管理，故意脱离范围的用例继续保留 negative／不支持，不放宽断言。[psutil 后代关系限制][PS1]、[进程所有权研究](upstream-process-resource-ownership-20261014.md)

## 正式 CLI 验收需要专门包含 Shell

真实 CLI 是 `.venv/bin/loom run <Application YAML> --output-format jsonl`，入口经 `app.cli.run()` 调用 `execute_app()`。fixture 应明确 `agent_runtime: smolagents`，包括 shell_tool、check_background_task、kill_background_task；managed 模式应由专用 OS job 的可信入口绑定 scope，然后执行同一 CLI／Application 逻辑。[CLI](../../src/app/cli.py)、[runner](../../src/app/runner.py)

现有 `run_native_write_shell_live.py` 的 `--smol` 控制组实际委托 `native_read_support.write_application()`，属于 smol read 控制；不能用其通过结果证明 smol shell 或后台进程收尾。[现有 live 脚本](../../tests/application_test/run_native_write_shell_live.py)、[fixture 分支](../../tests/application_test/native_write_shell_support.py)

后续验证至少覆盖：

| 正式入口与触发 | 独立外部结果 |
| --- | --- |
| Application ＋真实 SmolagentsRuntimeAdapter，前台普通命令 | 原输出／返回码；Shell 子进程属于任务组 |
| 同组 foreground timeout／stall／size limit | 原目标 PID 确实退出；Application 父组和另一工具未被误杀 |
| 前台 timeout promotion | 原 Popen 继续运行、task_id／artifact 保持，之后显式 kill 确认退出 |
| 显式后台与尾部 `&` | 正式 registry 控制工具可调用；Run 关闭前后资源归属清楚 |
| cleanup 抛错或进程拒绝退出 | 错误进入 Application outcome／cleanup evidence，原句柄仍可重试收尾，不能先补位 |
| root 先 exit，但孙进程持续写 output fd | task 展示状态与完整资源退出分开；外层 OS 所有者实际清理后才完成 invocation |
| 两个不同 Run 并发，关闭其中一个 | 另一个 Run 的具体进程、输出和 registry 状态不被清理 |
| Application 被 kill 后恢复 | 旧同组资源退出早于新 Runtime；自主 detached 用例不算支持 |

先用真实 Runtime 与受控 ModelTurnBinding 驱动这些无模型用例，不能把整个 Runtime 换成假实现。随后通过正式 DeepSeek v4.1 CLI 验收，同样提前准备 trace，保留 PID／创建时间／PGID、工具回执、输出与关闭证据。本轮未执行这些新用例，也不将 ShellProcess 的已有单测当作 managed Application 通过。

## 一手来源

[S1]: https://github.com/huggingface/smolagents/blob/12c1bc820eca50ace6f80a21d90426d41d74f845/src/smolagents/agents.py
[S2]: https://github.com/huggingface/smolagents/blob/12c1bc820eca50ace6f80a21d90426d41d74f845/src/smolagents/tools.py
[PS1]: https://github.com/giampaolo/psutil/blob/ea5b55605f857affa4e65fa27eb80f4f2bfebd63/psutil/__init__.py
[PS2]: https://github.com/giampaolo/psutil/blob/ea5b55605f857affa4e65fa27eb80f4f2bfebd63/docs/index.rst#kill-process-tree
[POSIX1]: https://pubs.opengroup.org/onlinepubs/9799919799/functions/kill.html
[POSIX2]: https://pubs.opengroup.org/onlinepubs/9799919799/functions/getpgid.html
