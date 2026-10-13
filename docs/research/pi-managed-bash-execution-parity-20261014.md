# Pi 受管理 Bash 保持官方执行行为

2026-10-14。**建议删除当前 Execa 接线，改用固定 Pi SDK 源码衍生的小型 Node spawn adapter。公开 `BashOperations` 继续作为接入点，官方 tool definition、capture、输出截断和回执保持原入口；底层只改变进程归属及工具停止目标。** 最新 Pi 1.2.0 仍未提供 detached 配置或公开等待 helper，升级无法删掉这层适配。[Pi 1.0.0][P1]、[Pi 1.2.0][P2]

Execa 是成熟项目，但其 Promise 等待 stdio 完成，与 Pi 的 shell exit 后输出 idle 合同不同。用它保留 Pi 行为，仍需自行补同样的 idle 状态机，还多出环境合并和超时默认值的差异。这是本次删除 Execa 的原因，不代表否定它的一般用途。[Execa 等待源码][E1]

本轮只读官方发布包、固定源码、实际安装文件和已有原始验证结果；没有改产品、运行模型或重复旧矩阵。待实现适配尚未通过正式 Runtime 验收。

## 实际源码和发布版本

| 项目 | 一手版本绑定 | 核对结果 |
| --- | --- | --- |
| 当前 Pi SDK | npm `@earendil-works/pi-coding-agent@1.0.0`；gitHead `a13d35a742c6ef8462812a28fbe1d8c8b7431c32` | 实际安装 bash.js 的 source map 与固定官方 bash.ts 逐字一致 |
| npm latest Pi | 2026-10-14 查询得到 1.2.0；tag commit `06a466935536f8c9bcacb2d5efc0fc021be1bdff` | 下载官方 tarball并校验 registry sha512；bash.js、bash.d.ts、bash.js.map 与已安装 1.0.0 逐字相同；官方 child-process.ts 也逐字相同 |
| 当前 Execa | 实际 worktree 安装 `execa@9.6.1`；发布 gitHead／tag peeled commit `84e1f36b2c31a463e3efbd37c7505da9438a05c9` | 本机 wait-subprocess、wait-stream、stdio、stdio-option、timeout 五个文件与固定官方源码逐字一致 |

Pi bash.js 的 SHA-256 是 `f32e87a42f66276b2eafaaf0848babf52523d37ab0fd448439586376747f03c4`；bash.ts 是 `de2b54d4f4bfbb844ce6d8077f63d715f7d8e80079dd9689b561e45b6679c81b`；child-process.ts 是 `cd324f5b9a1abadd85ab5a331750d475c8bd51ec65cae091ad9b416245ec85d4`。[当前 npm metadata][P0]、[latest 发布 metadata][P0L]、[Execa metadata][E0]

## Pi 的公开配置仍不足以保留默认 executor

1.0.0 与 1.2.0 的 `createLocalBashOperations(options)` 只接受 `shellPath`；内部固定 POSIX `detached=true`。`BashSpawnHook` 只修改 command、cwd、env，没有 spawn 参数。`waitForChildProcess` 虽在内部文件声明为 export，package root exports 和 package exports 均未将它公布为可依赖接口。内部文件可读，不能等同于公开 API。[固定 operations][P1]、[最新同源实现][P2]、[root exports][PI]、[package exports][PP]

公开 `BashOperations.exec` 明确允许宿主替换命令执行。保留这个扩展点，同时在本地附带固定来源的等待函数，比按绝对路径导入私有 helper 或 patch Node spawn 更稳定。这里不需要新建工具协议或 Agent 调度器。

## Execa 的公开选项能修 stdin，不能直接提供 Pi 的 idle 等待

已有独立验证保留两项实际 RED：

| 完整命令与同一超时配置 | 官方 Pi | 当前 Execa 候选 |
| --- | --- | --- |
| `cat >/dev/null; printf stdin-eof`，timeout 0.3 秒 | 8ms，exitCode 0，输出 stdin-eof | 309ms，timeout:0.3，无输出 |
| `sleep 2 & printf foreground-done`，timeout 0.3 秒 | 104ms，exitCode 0 | 2014ms 才返回 timeout:0.3 |

结果来自原始 [stdin 对照](../../.agentloom/validation/managed-scope-standards-review-20261014-v1/bash-behavior.json) 与 [后台命令对照](../../.agentloom/validation/managed-scope-standards-review-20261014-v1/bash-background.json)，本轮没有重跑。关闭或最终 controller 清理不能覆盖这些原结果。

Execa 默认将 stdin 设为 pipe。公开 `stdin:"ignore"` 可以对齐普通 Bash 的 stdin EOF；但 commandTransport 为 stdin 时，仍必须按 SDK argv 和输入写入逻辑处理。[Execa stdio 默认值][E2]、[Pi argv／stdio][P1]

Execa 的 `waitForSubprocessResult` 正常路径使用 `Promise.all` 等 root exit 和各 stdio promise；错误路径同样等各 stream 收尾。`buffer:false` 只关闭结果缓冲，`waitForSubprocessStream` 仍等待 `waitForStream`，后者默认等 `finished(stream)`。超时杀掉 shell，并不能让仍持有管道的孙进程立即释放 Execa Promise。[结果等待][E1]、[stdio 等待][E3]、[stream 等待][E4]

官方公开 API 中没有“root exit 后 100ms idle 即结束输出”的选项。`stdout:"ignore"` 或改成 inherited fd 会丢失现有 capture；改为 `await exit` 会丢失尾部输出。可以在 Execa 子进程的公开 exit／data 事件外层再维护 idle timer，届时 destroy stdout／stderr，让 Execa 的等待结束；这是源码上可行的推断，未在本轮实测。但 idle 状态、listener 清理与 timeout 顺序仍需维护，因此没有降低本项目必须拥有的适配复杂度。

还需要纠正两项源码已经确定的差异：

- Pi 在 spawn 前拒绝 timeout 0、负数、NaN／Infinity，以及超过 `2_147_483_647` 毫秒的值。Execa 明确把 timeout 0 作为禁用超时，候选直接映射数值无法保持前置拒绝。该项是固定源码判断；未将未运行的新用例写成实测。[Pi 验证][P1]、[Execa timeout][E5]
- Pi 使用传入 env，只有未传入时才调用 getShellEnv；Execa 默认 `extendEnv=true`，会再次合并 process.env。SDK 在构造工具环境时删除的 PI 字段可能因此重新出现。`extendEnv:false` 可以修正这一点，但不足以解决 idle 等待。[Execa env 选项][EAPI]、[Pi 环境构造][P1]

候选的 TERM 后 200ms 再 KILL 也与固定 Pi 默认立即 KILL 不同。保持业务执行行为时，不能将信号顺序默默交给 Execa 的默认或新增 grace 参数。[SDK 停止 helper][PS]、[Execa kill 默认][EK]

## 小型 adapter 应保留的源码范围

等待 helper 应取固定 1.0.0 source map／官方源码中的整个 `waitForChildProcess` 函数，按原字面保留，并保留其 `EXIT_STDIO_GRACE_MS=100`。它在 root exit 后开始 idle timer，每个新 stdout／stderr chunk 都重置 timer，两个流真正 end 或 child close 时可以提前完成。timer 结束会关闭流并返回 root exitCode。不能改成从 exit 起固定 100ms 的总等待，否则会再次丢掉持续到来的尾部输出。[完整 helper][PH]

exec 外壳保留 `createLocalShellOperations` 的输入输出合同：

- timeout 验证先于 abort 检查、cwd access 和 spawn；未配置时不设置默认 timeout。
- 调用公开 `getShellConfig()`，保留 commandTransport 的 argv／stdin 分支；普通 Bash stdin 使用 ignore。
- 传入 env 完整使用；只有 env 缺失时才构造与官方一致的 getShellEnv。
- stdout／stderr 的 Buffer 原样交给 onData，保留 AbortSignal、timeout 标记、wait 后的错误检查顺序、信号退出的 `128 + signal` 和 unknown 的失败映射。
- 保留 finally 中的 timer／listener 收尾；不复制 SDK 工具 schema、输出 accumulator 或 tool receipt 逻辑。

归属变化限定在两处：受管理 POSIX spawn 使用 `detached=false`；tool abort／timeout 的停止目标使用具体 child PID，不能对 Application 父组 `killpg`。默认 executor 的 detached tracking 可在受管理分支省去，完整后代由外层 OS job 所有者收尾。跨工具的错误、授权与业务完成规则继续走原入口。[operations 源码][P1]、[前一轮 scope 研究](pi-mcp-managed-process-scope-20261014.md)

由于工具停止范围发生了必要变化，这个 adapter 不能声称与 detached SDK 在任意后代行为上完全等价。确认退出的责任留在外层任务 scope；quiet background 后代仍存活时，tool exitCode 与输出 idle 都不能当成完整资源退出。现有 bash capture 的 `complete=null` 和覆盖限制应该继续保留。

`getShellEnv` 不在 root exports。其固定实现用 `getBinDir()` 加入 PATH，而 `getBinDir()` 正是 `join(getAgentDir(), "bin")`，`getAgentDir` 公开可用。可通过公开路径接口保留原算法：PATH 键大小写查找、系统 delimiter、去空项检查和避免重复 bin。不要只用 `process.env` 替代，也不要在已传入 env 上额外再合并。[shell 环境源码][PS]、[配置路径源码][PC]、[root exports][PI]

## 固定源码的 MIT 原文

Pi 1.0.0 固定 commit 的仓库根 LICENSE 原文如下，SHA-256 为 `0457f5bcec3b3b211605dfb5d1a49042fd638f3686a410fe099c24a25af13c48`。衍生适配应保存实际来源、源码 SHA 和这份原文，不能臆写版权人或只留一个 MIT 标签。[原始 LICENSE][PL]

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 验证仍需绑定新候选

新 adapter 至少需和固定官方 operations 对照普通 stdin EOF、root exit 后 quiet background、持续尾部输出、真实 stdout／stderr EOF、timeout 边界、预先 abort、spawn／cwd 错误和信号返回码。源函数逐字一致可证明衍生来源，但正式 bridge 是否按 scope 选择新 operations、capture 是否完整保留，还需经过实际 Application／Pi Runtime 入口验证。

当前 Execa 的两项原 RED 与 source snapshot 要保留。新候选通过才能替代实现；删依赖、typecheck 或单个普通命令通过都不能证明全日业务验收完成。

## 一手来源

[P0]: https://registry.npmjs.org/@earendil-works%2Fpi-coding-agent/1.0.0
[P0L]: https://registry.npmjs.org/@earendil-works%2Fpi-coding-agent/1.2.0
[P1]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/core/tools/bash.ts
[P2]: https://github.com/earendil-works/pi/blob/06a466935536f8c9bcacb2d5efc0fc021be1bdff/packages/coding-agent/src/core/tools/bash.ts
[PI]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/index.ts
[PP]: https://github.com/earendil-works/pi/blob/06a466935536f8c9bcacb2d5efc0fc021be1bdff/packages/coding-agent/package.json
[PS]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/utils/shell.ts
[PH]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/utils/child-process.ts
[PC]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/config.ts
[PL]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/LICENSE
[E0]: https://registry.npmjs.org/execa/9.6.1
[E1]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/lib/resolve/wait-subprocess.js
[E2]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/lib/stdio/stdio-option.js
[E3]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/lib/resolve/stdio.js
[E4]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/lib/resolve/wait-stream.js
[E5]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/lib/terminate/timeout.js
[EK]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/lib/terminate/kill.js
[EAPI]: https://github.com/sindresorhus/execa/blob/84e1f36b2c31a463e3efbd37c7505da9438a05c9/docs/api.md
