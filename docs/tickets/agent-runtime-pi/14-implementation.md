# 14 集成与交付记录

状态：最终代码候选 `f5b8a0f` 已完成验收，等待本记录提交后正式快进
main 并清理本次 worktree。机器可读结果见
[14-validation.json](14-validation.json)。

## 当前决策与改动

维护者明确撤销旧 YAML 兼容：旧根级 smol 字段静默忽略，不报错、不转换。仅 `runtime_options` 生效；新字段本身仍按适配器契约验证。删除公共 RuntimeDefinition 的 smol 过渡参数、旧字段 normalization、旧模板路径映射及无消费者校验 helper。

已迁移 132 个仓库 smol Agent 定义以及 2 个 system 配置；原全局 smart_summary=false 显式放入 smol 定义。Pi 定义保持自己的选项。同步中英文配置文档、当前规格和 framework skill；已完成票据的历史验收记录保留原事实，当前 A01 改为新格式 smol Application 验收。

12 新增 Pi 原生状态持久化、双日志恢复及压缩取消，详见 [12 实施](12-implementation.md)。官方 SDK 继续由锁定 npm 依赖自动安装在适配器私有 node_modules，不纳入 Git 或发行物。

## 针对性验证

- Pi 恢复、故障窗口、回执、compaction、Hook、进程和写入/Shell 组合专项
  118 passed；最终身份、Worker receipt 窗口和拒绝投影专项 30 passed。
- 新配置/Todo/template 专项 132 passed；配置/公共构造/CLI/生命周期/工厂集成 273 passed；smol 与 native-read Application 11 passed。
- Python 10 文件类型检查 0 errors / 0 warnings；TypeScript 构建通过。

早期配置集成运行未准备 CI 规定的 config/llm.yaml，导致 101 个缺 summary 模型配置的失败；按 `.github/workflows/tests.yml` 从公开 example 准备后，同组 273 项通过。未读取或覆盖 main 私有模型配置。

## 最终验收

- 与 CI 相同的完整测试：**4557 passed、1 skipped**，23 个第三方 warning，
  819.05 秒。
- 从 `f5b8a0f` 重建 sdist/wheel，在 Node 22.19.0 和 Python 3.12.13 下
  验证非 editable 的 Pi 13/13、Pi+code 9/9、smol 7/7。
- 首次在线安装因 PyPI 获取 `sse-starlette` 连接超时而失败；保留日志。
  uv 缓存已有锁定 hash 对应产物，新的输出目录以 `UV_OFFLINE=1` 执行相同
  脚本后 29/29 通过。
- 已安装 wheel 的真实 provider：正常 Pi Chat、Pi Responses、smol 3/3；
  fault recovery campaign 3/3。Pi 在 host 已提交 read、SDK 未收到结果时
  被杀死，新 Run 恢复后继续 write/bash，native receipt 总数保持 3。
- 四轮 Standards/Spec 审查最终均为 0 个实现阻塞。保留的判断项是
  `bridge/tools.ts` 与 `PiRuntime.run` 仍偏大；在接入下一种 native runtime
  前应拆分，但本次不为结构重构扩大已验收范围。

A01–A14 对应测试、发行摘要、artifact SHA、live 报告和失败历史见
[14-validation.json](14-validation.json)。候选后的文档提交不改变受检源码；
main 交付 SHA 与 worktree 清理结果在实际完成后补记。
