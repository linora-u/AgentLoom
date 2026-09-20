# 14 集成与交付记录

状态：最终集成验证中，尚未合入 main。

## 当前决策与改动

维护者明确撤销旧 YAML 兼容：旧根级 smol 字段静默忽略，不报错、不转换。仅 `runtime_options` 生效；新字段本身仍按适配器契约验证。删除公共 RuntimeDefinition 的 smol 过渡参数、旧字段 normalization、旧模板路径映射及无消费者校验 helper。

已迁移 132 个仓库 smol Agent 定义以及 2 个 system 配置；原全局 smart_summary=false 显式放入 smol 定义。Pi 定义保持自己的选项。同步中英文配置文档、当前规格和 framework skill；已完成票据的历史验收记录保留原事实，当前 A01 改为新格式 smol Application 验收。

12 新增 Pi 原生状态持久化、双日志恢复及压缩取消，详见 [12 实施](12-implementation.md)。官方 SDK 继续由锁定 npm 依赖自动安装在适配器私有 node_modules，不纳入 Git 或发行物。

## 针对性验证

- Pi 恢复与故障窗口 20 passed；原生压缩/恢复中取消 3 passed。
- 新配置/Todo/template 专项 132 passed；配置/公共构造/CLI/生命周期/工厂集成 273 passed；smol 与 native-read Application 11 passed。
- Python 10 文件类型检查 0 errors / 0 warnings；TypeScript 构建通过。

早期配置集成运行未准备 CI 规定的 config/llm.yaml，导致 101 个缺 summary 模型配置的失败；按 `.github/workflows/tests.yml` 从公开 example 准备后，同组 273 项通过。未读取或覆盖 main 私有模型配置。

## 待完成

最终候选全量 CI、A01–A14 证据映射、三种非 editable 干净安装、真实 provider smoke、两轴复审及 main 合入/本次 worktree 清理。此前票据的旧候选结果不替代本轮验收。
