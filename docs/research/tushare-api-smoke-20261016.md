# Tushare Token 与接口权限冒烟验证

验证日期：2026-10-16。目的：验证用户已填写的本地 Token 是否有效，以及本次新闻方案涉及的接口是否可访问。

## 结论

本地 Token 有效。交易日历及五类资讯接口均返回 HTTP 200、业务返回码 0，并取得非空数据。

验证从本地 `tushare.local.yaml` 的 `token` 字段读取凭据；没有用已有环境变量替换待验证 Token。请求使用官方 REST 地址 `https://api.waditu.com`，保持 TLS 校验和默认代理设置，不跟随重定向，不回显或另存密钥。

后续用户已指定使用公开开源库获取交易日历。本记录中的 `trade_cal` 仅保留为当次验证 Token 的事实，项目正式日历方案改为 `exchange_calendars`，不依赖该接口。

## 实际请求结果

| 类别 | 接口 | 测试范围 | 返回条数 |
| --- | --- | --- | --- |
| 交易日历 | `trade_cal` | 上交所，2025-01-02 | 1 |
| 新闻快讯 | `news` | 财联社，2025-06-20 08:00—08:10 | 2 |
| 长篇新闻 | `major_news` | 财联社，2025-06-20 08:00—09:00 | 10 |
| 新闻联播文字稿 | `cctv_news` | 2025-06-20 | 14 |
| 国家政策库 | `npr` | 2025-06-20 全日 | 1 |
| 央行货币政策报告 | `monetary_policy` | 2025-05-09 | 1 |

时间范围采用北京时间。只请求少量元信息以验证认证、接口权限及返回结构，没有下载正文数据集、报告 PDF 或运行新闻分析。

## 验证范围与后续工作

- 快讯和长篇仅抽查财联社，未逐一验证全部已选媒体。
- 本次使用历史小范围数据，不代表已验证最新数据延迟、全量完整性、分窗采集或报告正文提取。
- 本机安装的 Tushare SDK 为 1.4.29，默认请求地址为 HTTP；本次采用 HTTPS REST 直接验证，没有修改 SDK 或项目业务代码。曾尝试旧主机下的接口路径，返回 404；改用官方当前文档所列主机的 REST 根地址后验证成功。
- 验证通过不等于应用已经实现本地配置读取、环境变量注入或端到端采集与分析。这些要求和验收项已写入[正式 Spec](../specs/news-agent-tushare-preopen-spec.md)。
- 云端只通过环境变量注入 `TUSHARE_TOKEN` 的实际运行能力，仍需在功能实现后按 Spec 验收。

## 开源交易日历补充验证

按用户要求，正式日历来源改为 `exchange_calendars` 的 `XSHG`。本次在隔离环境中验证 4.13.2 版本，没有修改项目依赖或业务代码；本地日历查询没有使用 Token，也不需要远程请求。

| 分析交易日 | 库返回的前一交易日 | 场景 |
| --- | --- | --- |
| 2025-01-02 | 2024-12-31 | 跨年休市 |
| 2025-05-06 | 2025-04-30 | 五一长假 |
| 2025-10-09 | 2025-09-30 | 国庆与中秋长假 |
| 2026-10-16 | 2026-10-15 | 普通交易日 |

上述日期的开盘均为北京时间 09:30，前一交易日收盘均为 15:00。2026-10-01 与 2026-10-10 均判断为非交易日，未将调休周末当作证券交易日。

库声明的 `XSHG` 日期范围为 1990-12-03 至 2026-12-31；请求 2027 年日历时明确报超出覆盖范围。该范围是库的声明，不代表本次逐日核验了全部日期。后续实现应检查覆盖边界并更新依赖，不能静默按工作日补算。

## 官方依据

- [REST 与 SDK 调用说明](https://tushare.pro/wctapi/documents/40.md)
- [交易日历](https://tushare.pro/wctapi/documents/26.md)
- [新闻快讯](https://tushare.pro/wctapi/documents/143.md)
- [长篇新闻](https://tushare.pro/wctapi/documents/195.md)
- [新闻联播](https://tushare.pro/wctapi/documents/154.md)
- [国家政策库](https://tushare.pro/wctapi/documents/406.md)
- [央行货币政策报告](https://tushare.pro/wctapi/documents/465.md)
- [exchange_calendars 开源项目](https://github.com/gerrymanoim/exchange_calendars)
- [exchange_calendars PyPI](https://pypi.org/project/exchange-calendars/)
