"""Score historical event-to-ETF predictions against a local daily-price copy."""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import yaml

from .evaluate import PriceStore, _valid_number
from ..processing.prepare import _atomic_text
from ..processing.results import day_records, read_day_results, validate_initial_results
from ..settings import Settings, load_baseline_settings


LEVELS = ("利好", "利空")
APP_ROOT = Path(__file__).resolve().parents[1]


def analysis_fingerprint(settings: Settings, day: date) -> str:
    """Hash this day's actual inputs and online verification outputs."""
    day_root = settings.output_root / "days" / day.isoformat()
    manifest = json.loads((day_root / "manifest.json").read_text(encoding="utf-8"))
    from ..baseline import _model_signature
    model_path = APP_ROOT / "config" / "model.yaml"
    paths = [model_path, APP_ROOT / "config" / "system.yaml",
             APP_ROOT / "agent" / "初筛.md", APP_ROOT / "agent" / "精筛.md",
             APP_ROOT / "workflows" / "initial.yaml", APP_ROOT / "workflows" / "refined.yaml",
             APP_ROOT.parents[1] / "config" / "system.yaml", settings.etf_config,
             day_root / "manifest.json", day_root / "records.jsonl"]
    for batch in manifest["batches"]:
        paths.extend((day_root / batch["file"], settings.output_root / "results" / "initial"
                      / day.isoformat() / batch["file"].replace(".txt", ".json")))
    paths.extend(path for path in (day_root / "etf-universe-asof.json", day_root / "etf-exposure-asof.json",
                                  day_root / "records-original.jsonl") if path.is_file())
    paths.extend((APP_ROOT / "baseline.py", APP_ROOT / "settings.py",
                  APP_ROOT / "skill_reference.py", APP_ROOT / "processing" / "prepare.py",
                  APP_ROOT / "evaluation" / "prepare_etf_exposure.py"))
    paths.extend(path for path in sorted((APP_ROOT / "skills" / "news-catalyst").rglob("*"))
                 if path.is_file())
    exposure_root = load_baseline_settings().exposure_root
    if exposure_root is not None:
        exposure_manifest = exposure_root / "manifest.json"
        names_manifest = exposure_root / "stock_name_changes_manifest.json"
        exposure = json.loads(exposure_manifest.read_text(encoding="utf-8"))
        names = json.loads(names_manifest.read_text(encoding="utf-8"))
        paths.extend((exposure_manifest, names_manifest, Path(names["file"])))
        paths.extend(Path(entry["path"]) for entry in exposure["files"].values())
    directory = settings.output_root / "results" / "refined_chunks" / day.isoformat()
    paths.extend(path for path in sorted(directory.glob("chunk-*.json"))
                 if path.stem.removeprefix("chunk-").isdigit())
    digest = hashlib.sha256()
    for path in paths:
        name = str(path.resolve()).encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    batching = load_baseline_settings()
    data = json.dumps({"initial_max_tokens": batching.initial_max_tokens,
                       "refined_max_tokens": batching.refined_max_tokens,
                       "token_encoding": batching.token_encoding}, sort_keys=True).encode("ascii")
    digest.update(b"token_batching:" + data)
    digest.update(_model_signature().encode())
    return digest.hexdigest()


def prediction_contract(hold_days: int, sell_at: str, limits: dict[str, Decimal]) -> dict[str, Any]:
    return {"hold_days": hold_days, "sell_at": sell_at,
            "limits": {key: str(value) for key, value in limits.items()}}


def _mark_prediction_stale(settings: Settings, day: date) -> None:
    path = settings.output_root / "days" / day.isoformat() / "coverage.json"
    if path.is_file():
        coverage = json.loads(path.read_text(encoding="utf-8"))
        coverage["refined_result_state"] = "stale"
        coverage.pop("refined_result_mtime_ns", None)
        coverage.pop("event_etf_rows", None)
        _atomic_text(path, json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")


def _verify_prediction(settings: Settings, day: date, expected: dict[str, Any]) -> None:
    result = settings.output_root / "results" / f"{day.isoformat()}.jsonl"
    if not result.is_file():
        _mark_prediction_stale(settings, day)
        raise FileNotFoundError(f"{day}: 缺少精筛结果: {result}")
    meta = result.with_suffix(".meta.json")
    if not meta.is_file():
        _mark_prediction_stale(settings, day)
        raise ValueError(f"{day}: 缺少预测口径记录；请先用 run 重新生成对应预测")
    recorded = json.loads(meta.read_text(encoding="utf-8"))
    digest = hashlib.sha256(result.read_bytes()).hexdigest()
    if (recorded.get("sha256") != digest or recorded.get("prediction") != expected
            or recorded.get("analysis_fingerprint_version") != 9
            or recorded.get("analysis_fingerprint") != analysis_fingerprint(settings, day)):
        _mark_prediction_stale(settings, day)
        raise ValueError(f"{day}: 分析结果与当前新闻、提示词、模型或预测口径不一致；请先用 run 重新预测")
    review_digest = recorded.get("reviews_sha256")
    review_path = settings.output_root / "results" / "reviews" / f"{day.isoformat()}.json"
    if (not isinstance(review_digest, str) or not review_path.is_file()
            or hashlib.sha256(review_path.read_bytes()).hexdigest() != review_digest):
        raise ValueError(f"{day}: 候选事件判断及放弃原因记录缺失或变化")


def actual_level(value: Decimal, limits: dict[str, Decimal]) -> str | None:
    if value >= limits["positive"]:
        return "利好"
    if value <= limits["negative"]:
        return "利空"
    return None


def _price(row: dict[str, Any], field: str) -> Decimal:
    return Decimal(str(row[field])) * Decimal(str(row["adj_factor"]))


def _observation(store: PriceStore, code: str, entry_index: int, exit_index: int,
                 sell_at: str) -> tuple[Decimal | None, Decimal | None, str | None]:
    calendar = store.calendar
    prices = store.prices(code)
    if not prices:
        return None, None, "ETF 行情缺失"
    if calendar[entry_index] < min(prices):
        return None, None, "入场日前无 ETF 行情"
    for index in range(entry_index, exit_index + 1):
        session = calendar[index]
        row = prices.get(session)
        if row is None:
            return None, None, f"ETF 行情缺少交易日 {session}"
        needed = ("open", "adj_factor", "vol") if index == exit_index and sell_at == "open" else ("open", "close", "adj_factor", "vol")
        if any(not _valid_number(row.get(field)) for field in needed):
            return None, None, f"ETF 价格、复权因子或成交量无效 {session}"

    entry = _price(prices[calendar[entry_index]], "open")
    exit_price = _price(prices[calendar[exit_index]], sell_at)
    peak = entry
    worst = Decimal(0)
    last_close = exit_index if sell_at == "close" else exit_index - 1
    for index in range(entry_index, last_close + 1):
        mark = _price(prices[calendar[index]], "close")
        peak = max(peak, mark)
        worst = max(worst, (peak - mark) / peak)
    peak = max(peak, exit_price)
    worst = max(worst, (peak - exit_price) / peak)
    return exit_price / entry - 1, worst, None


def _entry_price_diagnostics(store: PriceStore, code: str, entry_index: int) -> dict[str, float | bool | None]:
    """Describe the entry gap and first session without changing the signal sample."""
    prices = store.prices(code)
    entry = prices[store.calendar[entry_index]]
    entry_open = _price(entry, "open")
    first_day_return = _price(entry, "close") / entry_open - 1
    overnight_gap: Decimal | None = None
    if entry_index:
        prior = prices.get(store.calendar[entry_index - 1])
        if prior is not None and all(_valid_number(prior.get(field))
                                     for field in ("close", "adj_factor")):
            overnight_gap = entry_open / _price(prior, "close") - 1
    return {
        "overnight_gap": float(overnight_gap) if overnight_gap is not None else None,
        "entry_day_return": float(first_day_return),
        "high_open_low_close": (overnight_gap > 0 and first_day_return < 0)
                                   if overnight_gap is not None else None,
        "low_open_high_close": (overnight_gap < 0 and first_day_return > 0)
                                      if overnight_gap is not None else None,
    }


def evaluate_day(
    settings: Settings,
    day: date,
    universe: dict[str, str],
    *,
    hold_days: int,
    sell_at: str,
    limits: dict[str, Decimal],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if hold_days < 1:
        raise ValueError("持有交易日必须大于零")
    if sell_at not in {"open", "close"}:
        raise ValueError("卖出时点只能是 open 或 close")
    if not limits["negative"] < 0 < limits["positive"]:
        raise ValueError("收益阈值必须满足 negative < 0 < positive")

    records = day_records(settings, day)
    initial = validate_initial_results(settings, day)
    if initial["complete_batches"] != initial["total_batches"]:
        raise ValueError(f"{day}: 初筛尚未完成全部批次")
    _verify_prediction(settings, day, prediction_contract(hold_days, sell_at, limits))
    events = read_day_results(settings, day, universe, records=records,
                              require_complete_initial=True)
    from ..baseline import _checked_events
    ledger = json.loads((settings.output_root / "results" / "reviews" / f"{day}.json").read_text())
    events = _checked_events(ledger["mapping_signals"], set(records), records, universe)
    day_root = settings.output_root / "days" / day.isoformat()
    manifest = json.loads((day_root / "manifest.json").read_text(encoding="utf-8"))
    coverage = json.loads((day_root / "coverage.json").read_text(encoding="utf-8"))
    store = PriceStore(settings.etf_root)
    entry_index = bisect.bisect_right(store.calendar, day)
    exit_index = entry_index + hold_days
    entry_date = store.calendar[entry_index].isoformat() if entry_index < len(store.calendar) else None
    exit_date = store.calendar[exit_index].isoformat() if exit_index < len(store.calendar) else None
    benchmark_return: Decimal | None = None
    if entry_date is not None and exit_date is not None:
        benchmark_return, _, _ = _observation(store, "510300.SH", entry_index, exit_index, sell_at)

    details: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    direction_hits = direction_total = 0
    relative: dict[str, dict[str, int]] = {
        "利好": {"hits": 0, "total": 0}, "利空": {"hits": 0, "total": 0},
    }
    for event in events:
        row: dict[str, Any] = {
            **event, "entry_date": entry_date, "exit_date": exit_date,
            "return": None, "return_decimal": None, "max_drawdown": None, "actual_level": None,
            "direction_hit": None, "relative_return": None,
            "overnight_gap": None, "entry_day_return": None,
            "high_open_low_close": None,
            "low_open_high_close": None,
            "exclusion": None,
        }
        if entry_date is None or exit_date is None:
            row["exclusion"] = "观察期不足"
        else:
            realized, drawdown, reason = _observation(store, event["etf_code"], entry_index, exit_index, sell_at)
            row["exclusion"] = reason
            if realized is not None and drawdown is not None:
                row["return"] = float(realized)
                row["return_decimal"] = str(realized)
                row["max_drawdown"] = float(drawdown)
                row.update(_entry_price_diagnostics(store, event["etf_code"], entry_index))
                row["actual_level"] = actual_level(realized, limits)
                if benchmark_return is not None:
                    row["relative_return"] = float(realized - benchmark_return)
                    if event["direction"] in relative and event["etf_code"] != "510300.SH":
                        side = relative[event["direction"]]
                        side["total"] += 1
                        side["hits"] += int(realized > benchmark_return if event["direction"] == "利好" else realized < benchmark_return)
                row["direction_hit"] = row["actual_level"] == event["direction"]
                row["auxiliary_one_percent_hit"] = (realized >= Decimal("0.01")
                    if event["direction"] == "利好" else realized <= Decimal("-0.01"))
                row["benchmark_return"] = float(benchmark_return) if benchmark_return is not None else None
                row["execution"] = {"daily_volume_positive": True,
                    "opening_fill_verified": False, "status": "daily_prices_only"}
                direction_total += 1
                direction_hits += int(row["direction_hit"])
                grouped[event["direction"]].append(row)
        if row["exclusion"]:
            excluded[row["exclusion"]] += 1
        details.append(row)

    summary: dict[str, Any] = {
        "day": day.isoformat(), "hold_days": hold_days, "sell_at": sell_at,
        "entry_date": entry_date, "exit_date": exit_date,
        "news_records": manifest["prepared_count"],
        "initial_batches": coverage["complete_batches"],
        "total_batches": coverage["total_batches"],
        "first_pass_selected_records": coverage["selected_ids"],
        "selected_records": json.loads((settings.output_root / "results" / "reviews" / f"{day}.json").read_text())["selected_records"],
        "total_event_etf_rows": len(events),
        "direction_hits": direction_hits, "direction_total": len(events),
        "scored_signals": direction_total, "scoring_complete": direction_total == len(events),
        "direction_hit_rate": direction_hits / len(events) if events else None,
        "market_reference": {"etf_code": "510300.SH",
                             "return": float(benchmark_return) if benchmark_return is not None else None,
                             "positive_outperformance": relative["利好"],
                             "negative_underperformance": relative["利空"]},
        "excluded": dict(excluded),
        "limits": {key: str(value) for key, value in limits.items()},
        "directions": [
            {"direction": level, "samples": len(rows),
             "mean_return": sum(row["return"] for row in rows) / len(rows) if rows else None,
             "max_price_drawdown": max((row["max_drawdown"] for row in rows), default=None)}
            for level in LEVELS for rows in [grouped[level]]
        ],
    }
    return summary, details


def write_evaluation(root: Path, day: date, summary: dict[str, Any], details: list[dict[str, Any]],
                     *, prices: Path | None = None) -> Path:
    directory = root / "reports" / "baseline"
    directory.mkdir(parents=True, exist_ok=True)
    stem = day.isoformat()
    details_text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in details)
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    pct = lambda value: "不可计算" if value is None else f"{value:.2%}"
    lines = [f"# {stem} 新闻事件 × ETF 基线评测", "",
             f"新闻：{summary['news_records']} 条；初筛：{summary['initial_batches']}/{summary['total_batches']} 批；入选：{summary['selected_records']} 条；事件 × ETF：{summary['total_event_etf_rows']} 行。",
             f"入场：{summary['entry_date']} 开盘；出场：{summary['exit_date']} {summary['sell_at']}；持有：{summary['hold_days']} 个交易日。",
             f"行情副本摘要：{summary.get('price_copy_sha256', '未记录')}。",
             "", f"方向命中率：{pct(summary['direction_hit_rate'])}（{summary['direction_hits']}/{summary['direction_total']}）",
             "实际三日收益≥+2%计利好命中，≤−1.5%计利空命中；开盘跳空与当日反转仅作诊断。"]
    reference = summary["market_reference"]
    positive = reference["positive_outperformance"]
    negative = reference["negative_underperformance"]
    lines.extend([
        f"沪深300 ETF 同窗口收益：{pct(reference['return'])}。",
        f"利好跑赢沪深300 ETF：{pct(positive['hits'] / positive['total'] if positive['total'] else None)}（{positive['hits']}/{positive['total']}）；"
        f"利空跑输沪深300 ETF：{pct(negative['hits'] / negative['total'] if negative['total'] else None)}（{negative['hits']}/{negative['total']}）。",
        "相对收益只是市场参照；黄金、跨境 ETF 与沪深300的风险来源不同，也不代表新闻造成了收益。",
    ])
    if summary["direction_total"] == 0:
        lines.append("方向命中率没有利好或利空判断；不可计算。")
    lines.extend(["", "| 预测信号 | 有效样本 | 平均实际收益 | 最大价格回撤 |",
                  "|---|---:|---:|---:|"])
    for grade in summary["directions"]:
        lines.append(f"| {grade['direction']} | {grade['samples']} | {pct(grade['mean_return'])} | {pct(grade['max_price_drawdown'])} |")
    lines.extend(["", f"缺行情或观察期不足：{sum(summary['excluded'].values())}。", "",
                  "## 排除原因", ""])
    lines.extend(f"- {reason}：{count}" for reason, count in sorted(summary["excluded"].items()))
    if not summary["excluded"]:
        lines.append("无。")
    lines.extend(["", "## 错例", ""])
    for row in details:
        if row["direction_hit"] is False:
            actual = row["actual_level"] or "未达信号阈值"
            lines.append(f"- {row['event_id']} · {row['etf_code']} · {row['direction']} → {actual}，实际收益 {pct(row['return'])}；新闻 ID：{', '.join(row['record_ids'])}；{row['summary']}")
    lines.extend(["", "价格最大回撤按持有窗口日线价格计算，不代表账户或组合回撤。", ""])
    path = directory / f"{stem}.md"
    outputs = {
        directory / f"{stem}-details.jsonl": details_text,
        directory / f"{stem}.json": summary_text,
        path: "\n".join(lines),
    }
    prepared: list[tuple[Path, Path]] = []
    marker_temporary: Path | None = None
    try:
        for target, content in outputs.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=f".{target.name}-", delete=False) as stream:
                stream.write(content)
                prepared.append((target, Path(stream.name)))
        marker = directory / f"{stem}-manifest.json"
        marker.unlink(missing_ok=True)
        for target, temporary in prepared:
            os.replace(temporary, target)
        manifest: dict[str, Any] = {
            "files": {target.name: hashlib.sha256(content.encode()).hexdigest()
                      for target, content in outputs.items()},
        }
        analysis = root / "results" / f"{stem}.jsonl"
        if analysis.is_file():
            manifest["analysis_sha256"] = hashlib.sha256(analysis.read_bytes()).hexdigest()
        reviews = root / "results" / "reviews" / f"{stem}.json"
        if reviews.is_file():
            manifest["reviews_sha256"] = hashlib.sha256(reviews.read_bytes()).hexdigest()
        if prices is not None:
            manifest["prices_root"] = str(prices.resolve())
            manifest["price_copy_sha256"] = summary["price_copy_sha256"]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=f".{stem}-manifest-", delete=False) as stream:
            marker_temporary = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(marker_temporary, marker)
    finally:
        for _, temporary in prepared:
            temporary.unlink(missing_ok=True)
        if marker_temporary is not None:
            marker_temporary.unlink(missing_ok=True)
    verify_evaluation_artifacts(root, day)
    return path


def verify_evaluation_artifacts(root: Path, day: date) -> None:
    """Reject an interrupted or mixed-version three-file evaluation report."""
    directory = root / "reports" / "baseline"
    stem = day.isoformat()
    marker = directory / f"{stem}-manifest.json"
    if not marker.is_file():
        raise ValueError(f"评测报告尚未完整发布: {marker}")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    expected = {f"{stem}-details.jsonl", f"{stem}.json", f"{stem}.md"}
    if set(manifest.get("files", {})) != expected:
        raise ValueError(f"评测报告清单不完整: {marker}")
    for name, recorded in manifest["files"].items():
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != recorded:
            raise ValueError(f"评测报告文件不一致: {path}")
    analysis = root / "results" / f"{stem}.jsonl"
    if analysis.is_file() != ("analysis_sha256" in manifest):
        raise ValueError(f"评测报告对应的精筛结果记录不完整: {analysis}")
    if "analysis_sha256" in manifest:
        if not analysis.is_file() or hashlib.sha256(analysis.read_bytes()).hexdigest() != manifest["analysis_sha256"]:
            raise ValueError(f"评测报告对应的精筛结果已变化: {analysis}")
    if "reviews_sha256" in manifest:
        reviews = root / "results" / "reviews" / f"{stem}.json"
        if (not reviews.is_file()
                or hashlib.sha256(reviews.read_bytes()).hexdigest() != manifest["reviews_sha256"]):
            raise ValueError(f"评测报告对应的候选事件记录已变化: {reviews}")
    summary = json.loads((directory / f"{stem}.json").read_text(encoding="utf-8"))
    has_price = "price_copy_sha256" in summary
    if has_price != ("price_copy_sha256" in manifest and "prices_root" in manifest):
        raise ValueError(f"评测报告对应的行情副本记录不完整: {marker}")
    if "price_copy_sha256" in manifest:
        if manifest["price_copy_sha256"] != summary["price_copy_sha256"]:
            raise ValueError(f"评测报告对应的行情副本摘要不一致: {marker}")
        prices = Path(manifest["prices_root"])
        copy_manifest = prices / "copy_manifest.json"
        if not copy_manifest.is_file():
            raise ValueError(f"评测报告对应的行情副本缺失: {copy_manifest}")
        files = json.loads(copy_manifest.read_text(encoding="utf-8"))["files"]
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if digest != manifest["price_copy_sha256"]:
            raise ValueError(f"评测报告对应的行情副本已变化: {prices}")
        for code, recorded in files.items():
            parquet = prices / f"ts_code={code}" / "data.parquet"
            if not parquet.is_file():
                raise ValueError(f"评测报告对应的行情副本已变化: {parquet}")
            with parquet.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != recorded["sha256"]:
                raise ValueError(f"评测报告对应的行情副本已变化: {parquet}")
