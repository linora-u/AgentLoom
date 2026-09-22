# Agent runtime 资产目录研究

日期：2026-09-21

状态：历史研究。最终实现采用当前虚拟环境的 `share/pi`，成功安装会替换旧 SDK；
详见 [`source-architecture-and-runtime-layout-refactor.md`](../specs/source-architecture-and-runtime-layout-refactor.md)。
下文保留的是方案比较过程，不代表当前运行合同。

## 结论

AgentLoom 的 Pi `node_modules` 与 `dist` 应从 Python package 和源码树移出，但**不应放入操作系统 cache**。它们应作为用户级、跨项目共享的已安装 runtime 资产，放在操作系统标准的 user data 目录：

```text
macOS   ~/Library/Application Support/AgentLoom/runtime-assets/...
Linux  ${XDG_DATA_HOME:-~/.local/share}/agentloom/runtime-assets/...
Windows %LOCALAPPDATA%\AgentLoom\runtime-assets\...
```

原因不是目录命名习惯，而是生命周期合同：用户显式执行 `loom runtime install pi` 后，Application 执行期不会运行 npm；删除这些文件会使 Pi 无法启动，而且已安装 runtime 应能离线运行。因此它们属于**安装资产**。只有可安全删除、缺失后能在正常启动流程中自动重建的内容才属于 cache。[XDG 规范把 cache 定义为 user-specific non-essential data](https://specifications.freedesktop.org/basedir/0.8/)，而 `platformdirs` 将 user data 映射到上述三个平台目录，并将 cache 映射到另一组目录。[platformdirs 平台路径表](https://platformdirs.readthedocs.io/en/latest/platforms.html)

若将来把合同改为“运行时发现缺失后可以可靠、原子、自动地重新 provision”，迁到 user cache 才合理。当前不要用 `AGENTLOOM_CACHE_ROOT` 表示必须存在的 runtime。

## 判断标准

| 资产 | 删除后的正确行为 | 目录语义 |
| --- | --- | --- |
| Python package 内的 bridge 源码、schema、manifest、lock | 由 Python 安装器管理 | package，只读 |
| Pi SDK `node_modules`、已构建 bridge `dist`、ready manifest | 删除后 runtime 不可用；需显式 repair/install | user data 中的安装目录 |
| npm 下载 tarball、构建中间文件 | 可删除；下次安装重新下载或重建 | npm/OS cache 或临时目录 |
| Run、checkpoint、session、memory | 项目与业务状态 | 项目 `.agentloom/`，保持现状 |

`node_modules` 本身不是 npm cache。npm 官方文档区分了安装根下的 `node_modules` 与独立的下载 cache；本地依赖应装入 package root 的 `node_modules`，npm cache 只负责下载内容。[npm folders](https://docs.npmjs.com/cli/v11/configuring-npm/folders/)

## 一手来源对比

### Pi

Pi CLI 本身通过 npm 全局安装。Pi package 的用户级安装位于 `~/.pi/agent/npm/` 和 `~/.pi/agent/git/`，项目级安装位于 `.pi/npm/` 和 `.pi/git/`；package 的 runtime dependencies 随 package 执行 `npm install`，位于相应安装根的 `node_modules`。这些路径是 Pi 的持久 app/package 目录，而非 OS cache。[Pi quick start](https://pi.dev/docs/latest)；[Pi package 路径与依赖](https://pi.dev/docs/latest/packages)

Pi 的 managed self update 也采用 staged、lockfile-backed release，验证成功后再激活，失败时保留当前 release。[Pi package manager CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/package-manager-cli.ts) 这与 AgentLoom 需要的“先完整安装和验证，再发布 ready 版本”一致。

### Claude Code

Claude Code 的 native CLI 是安装资产：macOS/Linux launcher 位于 `~/.local/bin/claude`，版本化 executable 位于 `~/.local/share/claude/versions/`；卸载时两者分别删除。[Claude Code setup](https://code.claude.com/docs/en/setup#auto-updates)

Claude Code 同时展示了 cache 与 data 的区别：marketplace plugin 的版本副本放在 `~/.claude/plugins/cache`；更新后旧版本保留一段 grace period，再清理。需要跨 plugin 版本保留的依赖或数据放在 `~/.claude/plugins/data/<id>`；官方示例把 `npm install` 产生的 `node_modules` 放进该 persistent data directory，并在最后一个安装 scope 卸载时删除。[plugin caching](https://code.claude.com/docs/en/plugins-reference#plugin-caching-and-file-resolution)；[persistent plugin data](https://code.claude.com/docs/en/plugins-reference#persistent-data-directory)

这说明“内容来自下载”不能单独决定放 cache；是否允许被清掉以及能否自动重建才决定目录语义。

### OpenAI Codex CLI

Codex CLI 本体由 npm/Homebrew 或 standalone installer 管理。standalone installer 把完整、按版本和平台隔离的 release 放在 `$CODEX_HOME/packages/standalone/releases/<version>-<target>`，通过 `current` symlink 激活；下载在临时目录完成，安装使用锁、staging 与验证。[Codex installer](https://github.com/openai/codex/blob/main/scripts/install/install.sh)

Codex 也提供另一个有效模式：可由产品重新 provision 的大型 primary runtime 放在 `~/.cache/codex-runtimes/`，卸载/cleanup 可以整体删除。[runtime cache policy](https://github.com/openai/codex/blob/a6fdb11eda992b706c5e6a21ae5fd8c7376f8e06/codex-rs/core-plugins/src/marketplace_policy.rs#L546-L572)；[Windows cleanup](https://github.com/openai/codex/blob/a6fdb11eda992b706c5e6a21ae5fd8c7376f8e06/codex-rs/windows-sandbox-service/src/package_lifecycle/cleanup.rs#L70-L112)

两种做法并不冲突：CLI release 是安装资产；primary runtime 是产品可恢复的受管 cache。AgentLoom 当前要求显式安装且 Application 运行期不下载，所以对应前一种生命周期。

### OpenCode

OpenCode 明确拆分 XDG data、cache、config 和 state，并把下载的 LSP/`rg` 放入 `<cache>/bin`，把动态 npm provider/package 放入 `<cache>/packages/<package-spec>/node_modules`。[Global paths](https://github.com/anomalyco/opencode/blob/c10134729dd2ce00beb18604ec91f10319f59a78/packages/core/src/global.ts#L1-L29)；[npm package cache](https://github.com/anomalyco/opencode/blob/c10134729dd2ce00beb18604ec91f10319f59a78/packages/core/src/npm.ts#L72-L137)

关键差异是 OpenCode 在 package 缺失时会在正常调用路径中重新安装，LSP 缺失时也会下载；整个 cache 可在 uninstall 时无条件删除。[OpenCode uninstall](https://github.com/anomalyco/opencode/blob/c10134729dd2ce00beb18604ec91f10319f59a78/packages/opencode/src/cli/cmd/uninstall.ts#L90-L101) 因此 OpenCode 是“可透明恢复则放 cache”的先例，不能直接套到 AgentLoom 当前合同。

### Aider

Aider 不维护独立 Node runtime。官方推荐 `uv tool install` 或 pipx，把 Aider 与完整 Python 依赖放进独立、持久的 tool environment；必要时 uv 还会安装独立 Python 3.12。[Aider installation](https://aider.chat/docs/install.html) uv 的 tool environment 默认位于 user data，下载和构建 cache 才位于 user cache。[uv tool environments](https://docs.astral.sh/uv/concepts/tools/#tool-environments)；[uv cache](https://docs.astral.sh/uv/concepts/cache/#cache-directory)

Aider 自己的 model metadata、help index 等可再生内容放在 `~/.aider/caches`，而 executable 与依赖不放在那里。[Aider model cache](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/models.py#L161-L220)

## AgentLoom 当前问题

当前 installer 默认以 Python package 内的 `bridge/` 为工作目录，直接运行 `npm ci`、`tsc`，并把 ready marker 写回该目录。[install.py](../../src/runtimes/pi/install.py) Transport 也从 package 相对路径寻找入口。[transport.py](../../src/runtimes/pi/transport.py)

这带来四个实际问题：

1. 安装后的 `site-packages` 可能不可写，系统 package、只读镜像和共享环境尤其如此。
2. pip/uv 并不知道后写入的 `node_modules`、`dist` 和 marker；Python distribution 的已安装文件本应由 `.dist-info/RECORD` 描述，外部写入会绕开升级与卸载管理。[Python installed projects specification](https://packaging.python.org/en/latest/specifications/recording-installed-packages/)
3. editable checkout 被约 286 MiB 的生成物污染，源码生命周期与安装资产生命周期耦合。
4. 单目录覆盖不能让旧进程与新 fingerprint 安全并存，也难以做原子升级和精确清理。

项目 `.agentloom/` 也不合适。它保存 Run、checkpoint、schedule、memory 等项目状态；Pi SDK 是跨项目共享的用户安装资产。把两者混合会让每个项目重复下载，并让项目清理误删全局 runtime。

## 建议目录与 key

用 `platformdirs.user_data_path("AgentLoom", appauthor=False)` 取得根目录，并提供只覆盖这一类资产的 `AGENTLOOM_RUNTIME_ASSETS_DIR`。不要复用项目状态的 `AGENTLOOM_RUNTIME_ROOT`。

```text
<agentloom-user-data>/
└── runtime-assets/
    └── pi/
        └── protocol-v2/
            └── sdk-0.79.4/
                └── <bridge-fingerprint>/
                    └── <os>-<arch>-nodeabi-<abi>/
                        ├── bridge/
                        │   ├── package.json
                        │   ├── package-lock.json
                        │   ├── dist/
                        │   └── node_modules/
                        └── ready.json
```

目录 key 至少包含：

- AgentLoom/Pi protocol major；
- Pi SDK 精确版本；
- bridge TypeScript、schema、manifest 和 lock 的 fingerprint；
- OS、CPU 架构与 Node module ABI。

最后一项不能省略：Pi 的依赖闭包含平台相关包，`node_modules` 不能默认在不同 OS、架构或 Node ABI 间安全复用。fingerprint 已精确描述 bridge 内容，因此不必再把完整 AgentLoom 版本放进路径；把 AgentLoom 版本写入 `ready.json` 便于诊断即可。

## 安装、运行与清理合同

### 安装

1. 从只读 Python package 读取 bridge 源码、schema、manifest 和 lock。
2. 在目标 data 根的 `.staging/<uuid>` 中复制输入并执行 `npm ci`、build 和 import/handshake 验证。
3. 写入 `ready.json`，记录 fingerprint、SDK、protocol、Node、OS/arch、完整性摘要和安装时间。
4. 通过同一文件系统内的 atomic rename 发布到最终 key；并发安装由 key 级锁串行化。
5. fingerprint 已存在且完整时直接复用，不运行 npm。

### Application 执行

- 只解析预期 key、验证 `ready.json` 并启动；不下载、不构建、不修改 capsule。
- 已安装 capsule 在断网和 npm cache 已清空时仍必须可运行。
- 缺失或损坏时明确要求 `loom runtime install pi` 或后续 `loom repair-runtime pi`，不在 Agent 执行中偷偷联网。

### 清理

- 提供 `loom runtime list` 显示版本、fingerprint、平台、大小和当前使用状态。
- 提供显式 `loom runtime prune`/`uninstall-runtime pi`；正常启动不自动删除旧 capsule。
- prune 只删除不再被当前 AgentLoom bridge 选择、且没有活跃 process lease 的 capsule。
- 安装开始时可自动删除已确认无锁的残留 staging；不能因一次失败破坏上一份 ready capsule。

### 离线

- “安装后离线可运行”是必须满足的合同。
- 首次离线安装需要预热 npm cache 或提供已校验的离线 bundle；这是安装命令能力，不能把网络需求推迟到 Application 执行期。
- npm 自身的 tarball cache 继续由 npm 管理，AgentLoom 不应把它与已安装 capsule 合并。

## 不采用的路径

| 路径 | 结论 | 原因 |
| --- | --- | --- |
| Python package / `site-packages` | 不采用 | 运行时写入未被 Python installer 管理，可能只读，升级/卸载边界不清 |
| Git checkout `src/runtimes/pi/bridge` | 不采用 | 污染源码树，CI/worktree 重复，无法代表 release 安装 |
| 项目 `.agentloom/` | 不采用 | 跨项目资产被重复安装，并与项目运行状态混合 |
| OS user cache | 当前不采用 | 删除后不能由正常运行透明重建，且破坏已安装后的离线可用性 |
| OS user data | 采用 | 与显式 install/repair/uninstall、持久离线运行和跨项目共享的合同一致 |

## 最终设计决策

1. `node_modules` 与 `dist` 移出源码树和 wheel 的可写区域。
2. 默认存入标准 user data 下的版本化、不可变 Pi capsule。
3. 使用 `AGENTLOOM_RUNTIME_ASSETS_DIR` 作为 CI、容器和集中离线部署的显式覆盖。
4. 保留源码/lock 在 Python package 中；安装在 staging 完成，验证后原子发布。
5. 只有 AgentLoom 将来承担缺失时自动 provision 的完整合同时，才重新评估迁入 user cache。
