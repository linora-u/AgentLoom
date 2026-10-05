from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .settings import Settings
from .processing.results import day_records, read_day_results


def write_day_report(
    settings: Settings,
    day: date,
    universe: dict[str, str],
    *,
    rows: list[dict[str, Any]] | None = None,
    records: dict[str, dict[str, Any]] | None = None,
) -> Path:
    if records is None:
        records = day_records(settings, day)
    if rows is None:
        rows = read_day_results(settings, day, universe, records=records)
    lines = [f"# {day.isoformat()} 事件与 ETF 影响", "", f"事件—ETF 条目：{len(rows)}", ""]
    for row in rows:
        refs = []
        for record_id in row["record_ids"]:
            record = records[record_id]
            member = next(item for item in record["members"] if item["record_id"] == record_id)
            refs.append(f"{record_id}（{member['source']}，{member['published_at'] or '时间未知'}）")
        lines.extend([
            f"## {row['event_id']} · {row['summary']}", "",
            f"- ETF：{row['etf_code']} {universe[row['etf_code']]} · {row['direction']}"
,
            f"- 依据：{row['reason']}",
            f"- 来源：{'；'.join(refs)}", "",
        ])
    path = settings.output_root / "reports" / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_year_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# {result['year']} 年新闻评测汇总", ""]
    if result["status"] != "complete":
        lines.extend(["全年预测尚未完整完成，胜率不可计算。", "",
                      f"缺少准备日期：{result.get('missing_prepared_days', 0)}；"
                      f"缺少有效日报：{len(result.get('missing_result_days', []))}。"])
    else:
        signals = result["directional_signals"]
        percent = lambda value: "—" if value is None else f"{value:.2%}"
        lines.extend(["T0 复权开盘入场，T+3 复权开盘退出；实际收益≥+2%/≤−1.5%计两类命中。", ""])
        for side, label in (("bullish", "利好"), ("bearish", "利空风险预警")):
            metrics = signals[side]
            lines.append(f"{label}：{metrics['signals']} 笔；命中率 {percent(metrics['hit_rate'])}；平均 ETF 收益 {percent(metrics['mean_etf_return'])}。")
        lines.extend(["", "年度汇总包含开发样本时不能替代独立验收；历史留出与真实开盘前的前瞻验证分开。"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
