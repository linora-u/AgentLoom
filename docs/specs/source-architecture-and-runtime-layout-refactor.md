# AgentLoom 源码架构与 Pi 开发运行环境重构

规格日期：2026-09-21
状态：设计已完成，待实施

## Problem Statement

AgentLoom 当前的源码目录没有稳定表达代码所有权。完整 Agent 执行引擎、外部协议适配、平台能力、工具实现、Studio 服务、命令入口和通用辅助代码分散在 `adapters`、`runtime`、`tools`、`tui_bridge`、`utils` 等位置。一级目录名称无法回答“谁拥有这段行为”，多个目录之间因此形成循环依赖、转发模块和重复实现。

现有 `tools` 同时包含 AgentLoom 平台工具、专业工具、smolagents 私有实现以及大量深层导入别名。部分工具已经由 Pi runtime 原生提供，部分工具在 smolagents runtime 内已有实际实现，顶层镜像只承担兼容导入。继续保留这些镜像会把旧目录结构固化为公共 API，并让调用方难以判断工具由平台还是具体 runtime 执行。

现有顶层 `utils` 隐藏了真实的依赖方向。动态导入、Shell sandbox、权限解析、Tree-sitter、模型诊断、路径解析和日志格式化都有明确的业务所有者，却通过模糊的共享目录被跨层调用。与此同时，少量真正重复的逻辑尚未在最近的共同所有者处集中。

Pi SDK 安装目前直接写入 Python package 或源码目录中的 bridge 目录。`node_modules` 约 286 MiB，生成产物与源码生命周期混在一起。安装、更新、运行和清理无法形成一个简单合同；开发者也无法仅通过删除当前虚拟环境清理 Pi SDK。项目当前只需要支持仓库开发环境，不需要为正式 wheel、系统级安装或跨项目共享设计资产目录。

此外，包导入会主动修改进程编码环境；旧 Textual Dashboard 已无产品入口；Studio bridge 含有 Application 业务编排；单一 CLI 文件聚集过多功能；runtime 的发现与装配依赖条件导入和自注册。这些问题共同导致源码结构难以理解、迁移和验证。

用户要求本次从职责和生命周期出发彻底重构，不保留旧 Python 深层导入、旧目录转发、旧 Dashboard 命令或 Pi runtime 命令兼容层。迁移可以分阶段执行，但所有阶段必须在同一期完成。

## Solution

将源码按业务所有权重组为以下顶层 module：

| Module | 所有权 |
| --- | --- |
| Application | Application 定义、装配、运行入口、工具加载与业务路径解释 |
| Configuration | 项目配置、模型配置与配置诊断 |
| Execution | 所有 Agent runtime 必须遵守的中立合同，以及平台共享的执行行为 |
| Runtimes / Pi | Pi 的完整执行循环、桥接、状态、恢复和原生工具接入 |
| Runtimes / smolagents | smolagents 的完整执行循环、私有状态、恢复和私有执行工具 |
| Integrations / LiteLLM | LiteLLM 协议与模型集成 |
| Integrations / MCP | MCP 协议与生命周期集成 |
| Integrations / LSP | LSP 协议与服务集成 |
| Tools | AgentLoom 工具 registry、平台工具和专业工具 |
| Schedules | 调度模型、生命周期与持久化 |
| Self-learning | 自学习模型、review 生命周期与持久化 |

删除职责已经被上述 module 吸收的 `adapters`、`encoding`、`ui`、`tui_bridge`、顶层 `utils` 和根部 scaffold module。保持源码目录直接映射为 `agentloom` Python package，不增加嵌套的 `src/agentloom` 目录，也不新增顶层 `studio_bridge`。

`execution` 只定义中立合同与共享执行行为。Pi 和 smolagents 分别实现这些合同。Application 拥有唯一的 composition root，根据 YAML 的 `agent_runtime` 从显式 registry 中选择实现。内置 runtime 不使用插件发现、自注册或为打破循环而设置的条件导入。

顶层 `tools` 只保留框架平台拥有的工具目录和 registry。Pi 使用官方 native tools；smolagents 的基础执行工具保留在 smolagents runtime 内。旧镜像、深层导入别名和无生产调用的工具被删除。真正重复的函数放到最近的共同业务所有者中，不创建新的顶层 `utils`、`common`、`shared` 或 `kernel`。

Pi SDK 只安装到当前仓库开发虚拟环境的 `share/pi`。运行环境以 SDK 精确版本、bridge 内容指纹、协议版本、操作系统、CPU 架构和 Node ABI 共同判断是否可复用。身份和完整性均一致时不执行 npm 或编译；任一不一致或安装损坏时，在 staging 中重新安装和验证，成功后替换当前目录并删除旧 SDK。删除仓库 `.venv` 即清除 Pi 安装资产。

根目录现有的 `agentloom-tui` 继续作为唯一 Studio/TUI 产品，并同时拥有 TypeScript 客户端和私有的薄 Python NDJSON 进程适配器：

```text
agentloom-tui/
├── src/       TypeScript/Bun 客户端
└── python/    Python NDJSON 适配器
```

Python 适配器只负责协议编解码、输入限制、并发、事件转发、错误投影和服务分发。现有 Python bridge 中的 Application 查询与修改、Run 投影、Schedule 修改和 Builder 编排分别迁回 Application、Schedules 及相应业务所有者。长驻 Python 进程继续支持 Builder 会话和流式事件；OpenCode Studio 使用的一次性 domain action 入口与长驻入口共享同一套薄分发层。Python 源码顶层不再保留第二个 Studio/TUI module。

CLI 统一提供 `loom runtime install pi`、`loom runtime status pi` 和 `loom runtime uninstall pi`。删除旧的 `loom runtime install pi`，不提供别名。旧 Dashboard 与 `loom dashboard` 完整删除；保留 `loom run`、调度和维护类命令，并将命令实现放回所属业务 module。

## User Stories

1. As an AgentLoom maintainer, I want each top-level module to express one clear owner, so that I can locate behavior without tracing forwarding imports.
2. As an AgentLoom maintainer, I want complete Agent engines separated from protocol integrations, so that runtime behavior is not mistaken for a thin adapter.
3. As an AgentLoom maintainer, I want `execution` to expose neutral contracts, so that Pi and smolagents can implement the same Application-facing seam.
4. As an AgentLoom maintainer, I want runtime selection assembled explicitly, so that startup dependencies are visible and testable.
5. As an AgentLoom maintainer, I want built-in runtimes registered in one composition root, so that conditional imports and self-registration do not create cycles.
6. As an AgentLoom maintainer, I want external protocols grouped as integrations, so that LiteLLM, MCP and LSP ownership is clear.
7. As an AgentLoom maintainer, I want Schedules to remain a first-class capability, so that its data model and lifecycle are not hidden under helpers.
8. As an AgentLoom maintainer, I want Self-learning to remain a first-class capability, so that review and persistence ownership remains local.
9. As an AgentLoom maintainer, I want the Studio-specific Python adapter owned by the existing TUI project and limited to transport concerns, so that presentation transport does not become a second Application service layer or another top-level Python module.
10. As an AgentLoom maintainer, I want Application queries and mutations implemented by Application services, so that Studio and CLI consume the same business behavior.
11. As an AgentLoom maintainer, I want the source directory mapped directly to the `agentloom` package, so that the repository does not gain an unnecessary nesting layer.
12. As an AgentLoom maintainer, I want old deep-import aliases deleted, so that the new module boundaries become the only supported API.
13. As an AgentLoom maintainer, I want tests that only assert alias identity removed, so that tests do not preserve migration debt.
14. As an AgentLoom maintainer, I want behavior tests migrated to canonical imports, so that real contracts remain covered after the move.
15. As an AgentLoom contributor, I want import dependencies to follow one direction, so that a local change does not recreate a first-level dependency cycle.
16. As an AgentLoom contributor, I want module moves separated from behavior changes during implementation, so that regressions can be located accurately.
17. As an AgentLoom contributor, I want the entire migration completed in one initiative, so that the repository does not remain in a mixed architecture.
18. As an AgentLoom contributor, I want dead compatibility code removed immediately after consumers move, so that temporary shims do not become permanent APIs.
19. As an AgentLoom tool author, I want every tool to have one business owner, so that authorization, execution and documentation point to one implementation.
20. As an AgentLoom tool author, I want platform tools separated from runtime-private tools, so that runtime-specific execution details do not leak into the platform registry.
21. As an AgentLoom Pi runtime user, I want Pi's official native tools used directly, so that AgentLoom does not maintain redundant implementations.
22. As an AgentLoom smolagents runtime user, I want its private execution tools kept with that runtime, so that their execution model remains coherent.
23. As an Application author, I want existing YAML tool names preserved, so that directory refactoring does not rewrite valid Application definitions.
24. As an Application author, I want framework platform tools available across runtimes when their contracts support it, so that governance remains owned by AgentLoom.
25. As an Application author, I want professional tools selected independently of the Agent runtime, so that code navigation and external integrations remain explicit capabilities.
26. As an AgentLoom maintainer, I want obsolete smolagents tool wrappers removed, so that upstream and project-owned decorator behavior cannot drift.
27. As an AgentLoom maintainer, I want unused tool helpers deleted, so that catalog size reflects supported behavior.
28. As an AgentLoom maintainer, I want repeated permission parsing owned by the permissions domain, so that policy interpretation has one implementation.
29. As an AgentLoom maintainer, I want repeated Shell configuration checks owned by Shell governance, so that security rules cannot diverge.
30. As an AgentLoom maintainer, I want Tree-sitter support owned by code navigation, so that syntax support is not a generic utility.
31. As an AgentLoom maintainer, I want model type diagnostics owned by configuration, so that model validation and diagnostics agree.
32. As an AgentLoom maintainer, I want Application path resolution owned by Application, so that callers do not invent path rules.
33. As an AgentLoom maintainer, I want Rich log formatting owned by runtime logging, so that presentation details have a clear home.
34. As an AgentLoom maintainer, I want dynamic Application tool loading owned by Application, so that arbitrary imports are not exposed through a generic helper.
35. As an AgentLoom maintainer, I want sandbox behavior owned by Shell governance, so that execution policy is not hidden in utilities.
36. As an AgentLoom maintainer, I want scaffold generation owned by Application, so that generated definitions follow Application contracts.
37. As an AgentLoom maintainer, I want shared logic extracted only after a second semantic caller exists, so that the architecture does not accumulate speculative abstractions.
38. As an AgentLoom library consumer, I want importing AgentLoom to leave stdout, stderr and locale settings unchanged, so that the package does not mutate my process unexpectedly.
39. As an AgentLoom protocol implementer, I want UTF-8 enforced at protocol boundaries, so that wire messages remain deterministic without global process mutation.
40. As an AgentLoom CLI user, I want terminal encoding handled by the CLI boundary, so that Unicode failures are reported without changing unrelated streams.
41. As an AgentLoom Studio user, I want one supported Studio shell in the existing TUI project, so that an undocumented legacy Dashboard or duplicate Python Studio package does not create another operational interface.
42. As an AgentLoom Studio user, I want Run inspection and mutations backed by Application services, so that Studio reflects runtime truth.
43. As an AgentLoom CLI user, I want noninteractive run and maintenance commands retained, so that scripts and CI do not depend on Studio.
44. As an AgentLoom CLI maintainer, I want the root command to register feature commands, so that one large command module does not own every workflow.
45. As an AgentLoom developer, I want one command group for runtime management, so that install, status and uninstall are discoverable together.
46. As an AgentLoom developer, I want an identical Pi installation reused, so that repeated setup does not run npm or TypeScript compilation.
47. As an AgentLoom developer, I want bridge changes to invalidate an installed Pi runtime even when the SDK version is unchanged, so that Python and TypeScript behavior cannot drift.
48. As an AgentLoom developer, I want protocol changes to invalidate an installed Pi runtime, so that incompatible envelopes cannot start.
49. As an AgentLoom developer, I want platform and Node ABI changes to invalidate an installed Pi runtime, so that native optional dependencies are not reused incorrectly.
50. As an AgentLoom developer, I want Pi installed under the repository virtual environment, so that deleting `.venv` removes the SDK and build outputs.
51. As an AgentLoom developer, I want Pi installation kept out of the source package tree, so that Git worktrees and Python modules remain clean.
52. As an AgentLoom developer, I want failed Pi installation to leave the previous valid runtime intact, so that retrying setup does not require manual recovery.
53. As an AgentLoom developer, I want successful Pi updates to delete the previous SDK, so that obsolete runtime assets do not accumulate.
54. As an AgentLoom developer, I want runtime status to explain installed identity and validation state, so that reinstall decisions are observable.
55. As an AgentLoom developer, I want runtime uninstall to remove the complete Pi asset directory, so that cleanup has one obvious operation.
56. As an AgentLoom Pi runtime user, I want Application execution to perform no npm installation or build, so that Agent runs do not introduce hidden network or mutation behavior.
57. As an AgentLoom Pi runtime user, I want the compiled bridge to resolve the SDK from its own installation root, so that Node module lookup is deterministic.
58. As an AgentLoom reviewer, I want forbidden legacy modules proven absent, so that completion is not inferred only from changed imports.
59. As an AgentLoom reviewer, I want both runtimes exercised through the Application seam, so that the new composition root is verified by behavior.
60. As an AgentLoom reviewer, I want runtime installation tested through its CLI seam, so that implementation details can change without rewriting the contract tests.
61. As an AgentLoom reviewer, I want import-time encoding behavior tested in a subprocess, so that process-global side effects are detected.
62. As an AgentLoom reviewer, I want deleted Dashboard behavior removed from tests and dependencies, so that obsolete product surface cannot silently return.
63. As an AgentLoom reviewer, I want source references, research checkouts and unrelated untracked files left untouched, so that architecture work does not clean the developer's workspace.

## Implementation Decisions

### Architecture and dependency direction

1. The final top-level Python package set is Application, Configuration, Execution, Runtimes, Integrations, Tools, Schedules and Self-learning. The existing `agentloom = "src"` package mapping remains; no nested package directory or top-level Studio bridge package is introduced.
2. Execution contains only contracts and behavior that are genuinely shared by multiple runtime adapters or owned by the platform. A concrete Agent loop, provider-specific state, recovery mechanism or native execution tool belongs to its concrete runtime.
3. Pi and smolagents each own a complete runtime implementation. Neither runtime imports the other, and the platform does not normalize them into a shared internal tool implementation.
4. Application owns a single explicit composition root. It maps the configured `agent_runtime` value to a built-in implementation. Runtime adapters depend on Execution contracts; Application depends on those contracts and performs final assembly.
5. Built-in runtime discovery does not use Python entry points, import-time registration or conditional imports. A future plugin system requires a separate specification.
6. LiteLLM, MCP and LSP are integrations because they adapt external protocols or services. They do not own Agent execution loops.
7. Schedules and Self-learning remain independent bounded contexts because each has its own model, lifecycle and persistence rules.
8. The existing root TUI project owns the private Python subprocess adapter required to reach AgentLoom's Python services. The adapter is not a public `agentloom` namespace and is limited to NDJSON encoding, bounded concurrency, event forwarding, error projection and dispatch. Application queries and mutations move to Application services; Run projections move to Application Run query services; schedule mutations move to Schedules; Builder orchestration moves to its Application owner.
9. The TUI's long-lived bridge entry and OpenCode Studio's one-shot domain action entry use the same private Python adapter and the same business services. They do not maintain separate catalog, validation or mutation rules.
10. The CLI root performs command registration and common error presentation. Feature command implementations live with their owning modules.
11. Dependency cycles are resolved by moving contracts toward the owner and performing construction in the composition root. Forwarding modules are not an accepted way to break a cycle.

### Deletions and compatibility policy

12. The previous adapter grouping, encoding package, legacy UI package, old TUI bridge name, top-level utilities package and root scaffold module do not exist in the final source tree.
13. The Textual Dashboard, its CLI command, its direct tests and the Textual dependency are deleted when no remaining production consumer exists. Studio remains the supported interactive shell.
14. Old Python deep-import paths, `sys.modules` aliases, forwarding modules and alias-identity tests are deleted. No deprecation window or compatibility package is provided.
15. Repository behavior tests move to canonical imports. Tests whose only purpose is to prove a legacy import aliases the canonical module are deleted.
16. YAML-visible tool names remain stable. The no-compatibility decision applies to Python module paths and removed CLI commands, not to valid Application definitions.
17. The old flat Pi installation command is removed. Runtime management exists only under the `runtime` CLI group.
18. Local reference repositories, research trees, unrelated untracked directories and existing project runtime evidence are not cleanup targets for this migration.

### Tools and shared functions

19. Top-level Tools contains the registry and selection control plane, AgentLoom platform tools, and professional tools. It does not mirror a concrete runtime's basic execution tools.
20. Pi uses the official Pi native read, edit, write and bash implementations, with AgentLoom authorization and journaling applied through the platform host contracts.
21. smolagents basic file, search, Shell, background and Todo execution remains private to the smolagents runtime where its execution model is implemented.
22. Smolagents mirror aliases in top-level Tools and Execution are deleted after all production and behavior-test consumers use canonical modules.
23. The local near-copy of the upstream smolagents tool decorator is removed. Its remaining production caller becomes an ordinary callable or uses the pinned upstream decorator when its contract matches.
24. Unused path-existence and quick-directory-list helpers are removed after confirming no production registry, YAML name or dynamic loader references them.
25. No top-level `utils`, `common`, `shared` or `kernel` module is created. Reuse is extracted on the second true semantic caller and placed in the nearest common owner under a domain-specific name.
26. Permission rule parsing belongs to Execution permissions. Shell configuration and security checks belong to Execution Shell governance. Tree-sitter support belongs to code navigation. Model diagnostics belong to Configuration. Application YAML path resolution and dynamic tool loading belong to Application. Rich prefix rendering belongs to Execution logging. Sandbox behavior belongs to Execution Shell governance. Scaffold generation belongs to Application.

### Encoding and process boundaries

27. Importing the root package has no stdout, stderr, locale or environment-variable side effects.
28. The existing encoding package is deleted. Protocol readers and writers explicitly use UTF-8, and the CLI handles terminal presentation at its boundary while preserving the caller's locale.
29. Error handling must not silently make stderr stricter than the process supplied. Encoding failures are localized to the boundary that performs the conversion.

### Pi development runtime assets

30. This specification supports only the repository development environment. Formal wheel installation, system Python, user data directories and cross-environment sharing are not designed or validated here.
31. The Pi runtime root is derived from the active Python environment's prefix and resolves to its `share/pi` directory. Under the supported workflow, this is the repository `.venv/share/pi` directory.
32. The installed tree contains the protocol schema, the AgentLoom bridge inputs, compiled bridge output, bridge-local `node_modules` and a ready manifest. Node resolves the Pi SDK from the bridge installation root.
33. Runtime identity includes the exact Pi SDK version, bridge source and dependency-lock fingerprint, protocol major version, operating system, CPU architecture and Node module ABI. The ready manifest records this identity and integrity information.
34. `runtime install pi` is idempotent. A complete ready installation with an identical identity is returned without invoking npm, TypeScript compilation or network access.
35. A missing, corrupt or nonmatching identity triggers installation into a sibling staging directory. Installation uses the committed dependency lock, compiles the bridge, verifies the SDK version, imports the SDK and bridge, and completes a protocol handshake before publication.
36. Publication replaces the single active `share/pi` tree only after all validation succeeds. The previous valid tree remains available if staging or validation fails.
37. A successful replacement deletes the previous SDK and bridge tree. AgentLoom does not retain old SDKs for rollback or compatibility.
38. Runtime execution only validates the ready manifest and launches the compiled entry point by absolute path. Application execution never runs npm, compiles TypeScript or repairs assets automatically.
39. `runtime status pi` reports whether the expected identity is ready, missing, corrupt or stale. `runtime uninstall pi` removes the complete `share/pi` tree. Removing the repository `.venv` is also a complete cleanup mechanism.
40. A matching SDK version alone is insufficient for reuse. Bridge, protocol, platform and Node ABI identity must also match.

### Migration sequencing

41. Work is delivered in reviewable stages while remaining one initiative: establish the final ownership map and dependency checks; move integrations and concrete runtimes; introduce explicit composition; relocate tools and shared functions; remove Encoding, Dashboard and compatibility modules; move Studio business logic to its owners and leave the thin adapter with the existing TUI project; restructure CLI; move Pi assets; then update documentation and run final validation.
42. Each stage removes its obsolete source after consumers and behavior tests move. The final tree contains no compatibility aliases, temporary forwarding modules or duplicate implementations.
43. Mechanical moves should preserve history where practical. Behavior changes such as encoding removal, runtime assembly and Pi installation are reviewed and tested as distinct changes within the initiative.
44. Current public Application behavior, YAML names, runtime state, checkpoint data and self-learning data are preserved unless this specification explicitly removes a surface.
45. The prior architecture specification remains authoritative for Application-definition semantics, Hook behavior, Run lifecycle and existing acceptance contracts. This specification supersedes its source ownership and directory layout wherever the two conflict.

## Testing Decisions

1. The primary behavior seam is an Application execution selected through `agent_runtime`. One controlled Application scenario is exercised with Pi and smolagents so runtime registry selection, construction, tool exposure and termination are verified through the highest shared seam.
2. Tests assert externally observable behavior: selected runtime identity, tool results, Run outcome, emitted records, CLI exit status, installed runtime state and process behavior. They do not assert that a compatibility module object is identical to a canonical module.
3. Existing Application, Pi, smolagents, tool gateway, Hook, checkpoint, schedules, self-learning and Studio-facing behavior tests are migrated to canonical imports rather than duplicated around the new layout.
4. An architecture test imports every supported top-level module from a cold interpreter and verifies that removed modules and deep aliases are absent. It also checks the allowed top-level dependency direction so the current first-level strongly connected component is eliminated.
5. A subprocess test imports the root package under a deliberately constrained locale and captures stream configuration before and after import. Import must not change stdout, stderr, locale variables or error handlers.
6. CLI tests use the existing Click runner seam. They verify the `runtime install`, `runtime status` and `runtime uninstall` command hierarchy, removal of the old flat install command, removal of the Dashboard command, and continued discovery of Run and schedule commands.
7. Pi installer tests use a temporary environment root and controlled Node/npm executables. The tests verify first installation, identical-install reuse, invalidation on every identity component, corruption repair, concurrent installation serialization, output redaction and failure recovery.
8. Pi update tests begin with a valid old tree. A failed staged installation must preserve it; a successful staged installation must publish the new identity and remove the old SDK. No test expects two completed SDK versions to remain.
9. Pi transport tests resolve the compiled entry through the runtime asset resolver and start Node from the returned absolute path. They verify bridge-local SDK resolution and a real handshake without consulting package-relative `node_modules`.
10. Tool catalog tests verify that top-level Tools exposes only registry, platform and professional capabilities; Pi native tools and smolagents private tools remain owned by their runtimes. Existing YAML-visible tool names continue to resolve.
11. Studio adapter tests continue at the NDJSON request/response seam and live with the existing TUI project. Python business behavior is tested through Application, Application Run query and Schedules services; adapter tests cover framing, dispatch, concurrency, cancellation and error projection.
12. Dashboard tests and tests that only assert deleted compatibility imports are removed. Their removal is verified by the absence of the command, module and unused dependency rather than replacement tests for deleted code.
13. Execution, schedule, self-learning and Application persistence tests verify that project `.agentloom` data remains in place and is not mixed with Pi SDK assets.
14. The final validation runs the relevant unit and integration suites after all moves, followed by controlled real Application executions for both built-in runtimes. Existing acceptance evidence conventions are reused; model self-report is not treated as proof of tool behavior.
15. Formal wheel installation and clean-install profile tests are not acceptance gates for the Pi asset-location decision in this specification. Tests may still be updated for canonical imports when needed by the source migration, but no formal-install path is added.
16. Testing stops once the canonical imports, dependency direction, two runtime paths, retained platform behavior and Pi lifecycle are sufficiently verified. Deleted compatibility surfaces are not recreated solely to satisfy historical tests.

## Out of Scope

- Formal wheel, pip, uv tool, system Python or OS package installation of Pi runtime assets.
- OS user data and cache placement, `platformdirs`, or an environment-variable override for a global runtime asset root.
- Sharing one Pi SDK installation across multiple repositories, virtual environments or AgentLoom versions.
- Supporting old AgentLoom environments after a successful SDK replacement.
- Python entry-point runtime plugins or third-party runtime discovery.
- A common implementation of Pi and smolagents basic execution tools.
- A replacement for the deleted Textual Dashboard.
- New Studio product features or another interactive CLI product.
- Changes to valid YAML tool names, Application definition semantics, checkpoint formats, Run evidence formats or self-learning persistence.
- Moving the package into a nested `src/agentloom` layout.
- Cleaning local research repositories, unrelated untracked files, historical Run evidence or checkpoint state.
- Automatic Pi SDK installation during normal Application execution.
- Windows support work beyond preserving code that already functions there.

## Further Notes

The term **runtime** in this specification means a complete Agent execution engine such as Pi or smolagents. The term **integration** means a boundary to an external model or protocol service such as LiteLLM, MCP or LSP. A **platform tool** is owned by AgentLoom and may be offered to more than one runtime. A **runtime-private tool** follows the concrete runtime's execution model. A **Pi runtime asset** is the installed SDK and compiled bridge under the current development virtual environment.

The repository research note comparing Pi, Claude Code, Codex CLI, OpenCode and Aider remains useful background on asset lifecycles. Its earlier user-data recommendation is not normative for this implementation; the later product decision limits the current scope to the repository development environment.

This specification intentionally favors deletion over compatibility. A successful result is a smaller dependency graph with one implementation and one canonical import for each retained behavior, not a new layout wrapped around the old one.
