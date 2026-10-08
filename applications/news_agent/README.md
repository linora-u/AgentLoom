# news_agent

流程只有一次初筛和一次精筛：初筛选择候选原文，精筛自主核实并给出最终利好、利空或放弃结论，之后保存结果及计分。

精筛使用 news-catalyst Skill，行业 reference 按需读取。程序不检查必须调用哪些审核工具、读取哪些 reference 或打开哪些 URL；来源、首披、历史版本、事前预期、ETF 暴露和持续影响由精筛 Agent 核实，证据和放弃理由随结果保存。

同一决策时段同一 ETF 只保留一个方向：精筛只要给出利空，最终就按利空；没有利空时保留利好。程序直接应用该规则，不调用来源复核、事实合并或净方向 Agent。跨批次及休市新闻通过同一入场日应用同一规则。

模型输出只包含 reviews，使用批内整数 record_numbers。程序映射回准确原 ID，并从推荐项提取信号；全部候选都保留审核或放弃记录。原始事件×ETF 映射保存为 mapping_signals，用于完整诊断计分，最终每日结果和正式名单单独保存。

数据位于 data/：news/raw/ 为五类原文，etf_dk_merge/ 为评测行情，etf_exposure/ 为历史持仓线索；evaluation/ 保存输入、结果、失败、用量和报告。原文完整分段，保持来源及字符区间，不缩减总新闻。所有初筛批次只分类一次，不再复读全部剔除项。

Git 跟踪新闻原文与来源文件、etf_universe.yaml、etf_dk_merge/ 和 etf_exposure/；评测中间数据、运行缓存和输出继续忽略。持仓与股票名称清单按所在数据目录解析相对文件路径，可随仓库迁移。

从 AgentLoom 根目录运行：

```bash
export PYTHONPATH=applications:src
.venv/bin/python -m news_agent.evaluation.replay prepare
.venv/bin/python -m news_agent.evaluation.replay dev
.venv/bin/python -m news_agent.evaluation.replay freeze
.venv/bin/python -m news_agent.evaluation.replay validate
```

预算、并发、月份和收益门槛来自 config/settings.yaml；模型、推理和重试来自 config/model.yaml。模型及 xhigh 设置保持不变。初筛与精筛缓存分别根据本阶段输入、模型、规则及运行依赖判断有效性；改精筛 Skill 不触发初筛重跑，正式验收仍记录完整方法。

收益从下一有效交易日 T0 复权开盘至 T+3 复权开盘：利好达到 +2%、利空达到 −1.5%分别计命中，边界包含等号。利空是风险预警。正式名单在读取收益前固定；失败和缺价不从分母消失。正式信号以入场日×ETF 计数，不同 ETF 分别保留；相关 ETF 信号不能视为统计上相互独立的证据。

程序只记录和计算客观结果：逐日利好/利空数量、收益、命中率、样本量、置信区间、放弃原因以及输入、预测和计分的缺失情况。无信号的日期与缺少预测记录的日期分别报告。报告交给主 Agent 审核漏选、过度拒绝、事实核实、ETF 映射和正常预测误差，确认规则缺陷后再修改 Prompt 或 Skill。阶段数量及胜率目标由当前 Goal 表达，程序不设置固定验收门槛，也不自动判断业务通过或失败。

开发月先完成全部预测再计分，后续按已配置的月份运行并报告；运行前确定日历、收益口径和方法版本，同一轮保持一致。运行日志和永久追踪保留实际请求、重试、失败、耗时、token 和用量未知项。直接维护当前实现，删除旧流程与兼容分支，不保留旧代码副本。
