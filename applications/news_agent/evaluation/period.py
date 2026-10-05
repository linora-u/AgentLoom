"""Freeze entry-session net predictions before scoring the formal ETF list."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .baseline import actual_level, verify_evaluation_artifacts
from ..processing.prepare import _atomic_text
from ..settings import load_baseline_settings

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "evaluation"
AUDIT_FORMAT = "formal_entry_etf_bearish_priority"


def dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _day_root(root: Path | Mapping[str, Path], day: str) -> Path:
    if isinstance(root, Path):
        return root
    preferred = root.get(day[:7])
    if preferred is not None and (preferred / "days" / day / "manifest.json").is_file():
        return preferred
    for candidate in root.values():
        if (candidate / "days" / day / "manifest.json").is_file():
            return candidate
    raise ValueError(f"{day}: missing source root for contiguous period")


def _signal_inputs(root: Path | Mapping[str, Path], start: date, end: date) -> tuple[list[dict], dict]:
    signals: list[dict] = []
    hashes = {}
    for day in dates(start, end):
        path = _day_root(root, str(day)) / "results" / f"{day}.jsonl"
        hashes[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
        review_path = _day_root(root, str(day)) / "results" / "reviews" / f"{day}.json"
        hashes[str(review_path.resolve())] = hashlib.sha256(review_path.read_bytes()).hexdigest()
        ledger = json.loads(review_path.read_text())
        signals.extend({**row, "day": str(day)} for row in ledger["mapping_signals"])
    return signals, hashes


def _entry_audit_path(root: Path, entry_dates: list[str]) -> Path:
    digest = hashlib.sha256(json.dumps(entry_dates).encode()).hexdigest()[:16]
    return root / "reports" / "entry_signals" / f"{digest}.json"


def _audit_digest(days: dict) -> str:
    return hashlib.sha256(json.dumps(days, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_entry_audit(root: Path, start: date, end: date, entry_dates: list[str]) -> dict:
    """Freeze one decision per entry date and ETF without reading outcomes."""
    import pyarrow.parquet as pq
    from .evaluate import _date
    from .. import baseline as runner

    config = load_baseline_settings()
    calendar = sorted({_date(item) for item in pq.ParquetFile(
        config.prices / "ts_code=510300.SH" / "data.parquet").read(
        columns=["trade_date"])["trade_date"].to_pylist()})
    signals, hashes = _signal_inputs(root, start, end)
    method = runner._model_signature()
    path = _entry_audit_path(root, entry_dates)
    if path.is_file():
        frozen = json.loads(path.read_text())
        if (frozen.get("format") != AUDIT_FORMAT or frozen.get("prediction_hashes") != hashes
                or frozen.get("method_fingerprint") != method or frozen.get("entry_dates") != entry_dates
                or frozen.get("formal_list_sha256") != _audit_digest(frozen.get("days", {}))):
            raise ValueError("冻结的正式名单与当前版本或输入不一致；必须另起版本")
        return frozen
    by_entry: dict[str, list[dict]] = defaultdict(list)
    for signal in signals:
        index = bisect_right(calendar, date.fromisoformat(signal["day"]))
        if index < len(calendar) and str(calendar[index]) in entry_dates:
            by_entry[str(calendar[index])].append(signal)
    result: dict[str, Any] = {"format": AUDIT_FORMAT, "entry_dates": entry_dates,
        "prediction_hashes": hashes, "method_fingerprint": method,
        "counting_unit": "entry_date_x_etf",
        "days": {}}
    for entry in entry_dates:
        part = by_entry.get(entry, [])
        per_code: dict[str, list[dict]] = defaultdict(list)
        for row in part:
            per_code[row["etf_code"]].append(row)
        nets = []
        for decision in runner.merge_etf_decisions(part):
            code = decision["etf_code"]
            rows = per_code[code]
            chosen = [row for row in rows if row["direction"] == decision["direction"]]
            nets.append({**decision, "signal_id": f"{entry}:{code}", "entry_date": entry,
                "event_ids": sorted({f"{row['day']}:{row['event_id']}" for row in chosen}),
                "all_fact_assessments": rows,
                "mapping_refs": [{"day": row["day"], "event_id": row["event_id"],
                                  "etf_code": code, "direction": row["direction"]} for row in rows]})
        formal = nets
        result["days"][entry] = {"formal_signals": formal, "all_net_signals": nets,
            "recommended_etfs": sorted(per_code), "decision_rule": "refined_bearish_priority",
            "countable_signals": len(formal),
            "countable_bullish_signals": sum(row["direction"] == "利好" for row in formal),
            "countable_bearish_signals": sum(row["direction"] == "利空" for row in formal),
            "bearish_priority_etfs": [code for code, rows in per_code.items()
                if {row["direction"] for row in rows} == {"利好", "利空"}],
            "reported_events": len({(row["day"], row["event_id"]) for row in part})}
    result["formal_list_sha256"] = _audit_digest(result["days"])
    _atomic_text(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def _wilson(hits: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = hits / total
    center = (p + z*z / (2*total)) / (1 + z*z/total)
    radius = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / (1 + z*z/total)
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _is_scored(row: dict) -> bool:
    return (row.get("exclusion") is None and not row.get("late", False)
            and isinstance(row.get("return"), (int, float)) and math.isfinite(row["return"]))


def _hit(row: dict, limits: dict[str, Decimal]) -> bool:
    value = Decimal(row.get("return_decimal") or str(row["return"]))
    return actual_level(value, limits) == row["direction"]


def direction_metrics(rows: list[dict]) -> dict:
    limits = load_baseline_settings().thresholds
    scored = [row for row in rows if _is_scored(row)]
    hits = sum(_hit(row, limits) for row in scored)
    actual = [row["return"] for row in scored]
    signed = [row["return"] * (1 if row["direction"] == "利好" else -1) for row in scored]
    losses = [value for value in actual if value < 0]
    adverse = [value for value in signed if value < 0]
    relative = [row["relative_return"] for row in scored if row.get("relative_return") is not None]
    benchmark = [row["benchmark_return"] for row in scored if row.get("benchmark_return") is not None]
    auxiliary_hits = sum((row["return"] >= .01 if row["direction"] == "利好" else row["return"] <= -.01) for row in scored)
    return {"signals": len(rows), "scored_signals": len(scored), "hits": hits,
        "hit_rate": hits / len(rows) if rows else None, "hit_rate_wilson_95": _wilson(hits, len(rows)),
        "distinct_entry_dates": len({row["entry_date"] for row in rows if row.get("entry_date")}),
        "unscored_cases": [row for row in rows if not _is_scored(row)],
        "late_cases": [row for row in rows if row.get("late")],
        "mean_etf_return": sum(actual) / len(actual) if actual else None,
        "mean_etf_loss": sum(losses) / len(losses) if losses else None,
        "worst_etf_return": min(actual, default=None),
        "mean_signed_return": sum(signed) / len(signed) if signed else None,
        "mean_adverse_move": sum(adverse) / len(adverse) if adverse else None,
        "worst_adverse_move": min(adverse, default=None),
        "mean_relative_return": sum(relative) / len(relative) if relative else None,
        "mean_benchmark_return": sum(benchmark) / len(benchmark) if benchmark else None,
        "relative_samples": len(relative),
        "auxiliary_one_percent": {"hits": auxiliary_hits, "signals": len(rows),
                                   "hit_rate": auxiliary_hits / len(rows) if rows else None},
        "opening_reversal_cases": [row for row in scored if row.get(
            "high_open_low_close" if row["direction"] == "利好" else "low_open_high_close") is True],
        "scoring_complete": len(scored) == len(rows) and len(relative) == len(rows),
        "execution": {"opening_fill_verified": False, "basis": "daily OHLCV only"},
        "bullish_cost_scenarios": [{"per_side_bps": bps, "samples": len([r for r in scored if r["direction"] == "利好"]),
            "mean_net_return": (sum((1+r["return"])*(1-bps/10000)/(1+bps/10000)-1
                for r in scored if r["direction"] == "利好") / sum(r["direction"] == "利好" for r in scored)
                if any(r["direction"] == "利好" for r in scored) else None)} for bps in (0, 10, 30)],
        "return_definition": "T0 adjusted open to T+3 adjusted open; bearish signed returns are risk diagnostics, not short profits",
        "confidence_interval_note": "Wilson 95%; observations may share events and overlapping windows; sample size and historical hit rate do not establish a stable future hit rate"}


def _scored_net(net: dict, mappings: list[dict]) -> dict:
    matching = [row for row in mappings if row.get("entry_date") == net["entry_date"]
                and row["etf_code"] == net["etf_code"]]
    if not matching:
        from .evaluate import PriceStore
        from .baseline import _observation, _entry_price_diagnostics
        config = load_baseline_settings()
        store = PriceStore(config.prices)
        entry_index = store.calendar.index(date.fromisoformat(net["entry_date"]))
        exit_index = entry_index + config.hold_days
        row = {"return": None, "return_decimal": None, "relative_return": None,
               "exclusion": "观察期不足", "entry_date": net["entry_date"]}
        if exit_index < len(store.calendar):
            value, drawdown, error = _observation(store, net["etf_code"], entry_index, exit_index, "open")
            benchmark, _, _ = _observation(store, "510300.SH", entry_index, exit_index, "open")
            row.update({"exclusion": error, "exit_date": str(store.calendar[exit_index]),
                        "return": float(value) if value is not None else None,
                        "return_decimal": str(value) if value is not None else None,
                        "max_drawdown": float(drawdown) if drawdown is not None else None,
                        "benchmark_return": float(benchmark) if benchmark is not None else None,
                        "relative_return": float(value-benchmark) if value is not None and benchmark is not None else None})
            if value is not None:
                row.update(_entry_price_diagnostics(store, net["etf_code"], entry_index))
        return {**row, **net}
    row = matching[0]
    if any((item.get("return_decimal"), item.get("return"), item.get("exclusion"), item.get("entry_date")) !=
           (row.get("return_decimal"), row.get("return"), row.get("exclusion"), row.get("entry_date")) for item in matching):
        raise ValueError("同一入场日 ETF 的映射收益不一致")
    return {**row, **net}


def score(root: Path | Mapping[str, Path], start: date, end: date,
          *, entry_dates: list[str] | None = None) -> dict:
    from .. import baseline as runner
    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    config = load_baseline_settings()
    method = runner._model_signature()
    rows: list[dict[str, Any]] = []
    days: list[dict[str, Any]] = []
    contract = None
    for day in dates(start, end):
        source_root = _day_root(root, str(day))
        verify_evaluation_artifacts(source_root, day)
        folder = source_root / "reports" / "baseline"
        summary = json.loads((folder / f"{day}.json").read_text())
        current = (summary["hold_days"], summary["sell_at"], summary["limits"])
        expected = (3, "open", {key: str(value) for key, value in config.thresholds.items()})
        if current != expected or (contract is not None and current != contract):
            raise ValueError(f"{day}: 评测不符合冻结的三日开盘及命中阈值口径")
        contract = current
        details = [json.loads(line) for line in (folder / f"{day}-details.jsonl").read_text().splitlines() if line]
        rows.extend({**item, "day": str(day)} for item in details)
        manifest = json.loads((source_root / "days" / str(day) / "manifest.json").read_text())
        review_path = source_root / "results" / "reviews" / f"{day}.json"
        reviews = json.loads(review_path.read_text()) if review_path.is_file() else {}
        candidates = [item for item in reviews.get("reviews", []) if item["stage"] == "chunk"]
        days.append({"day": str(day), "raw_source_news": manifest.get("raw_count", manifest["prepared_count"]),
            "candidates": manifest["prepared_count"], "batches": len(manifest["batches"]),
            "selected": summary["selected_records"], "all_mapping_signals": len(details),
            "reported_events": len({item["event_id"] for item in candidates if item["event_id"] is not None}),
            "abstentions": [item for item in candidates if item["decision"] == "放弃"],
            "initial_passes": 1, "refinement_complete": bool(reviews),
            "unavailable_future_revisions": manifest.get("unavailable_future_revisions", 0)})
    entries = sorted(entry_dates or [])
    if len(entries) != len(set(entries)):
        raise ValueError("固定入场交易日不能重复")
    formal_rows = []
    net_rows = []
    coverage = []
    frozen_hashes = {}
    for month in sorted({entry[:7] for entry in entries}):
        month_entries = [entry for entry in entries if entry.startswith(month)]
        source_root = root if isinstance(root, Path) else root[month]
        path = _entry_audit_path(source_root, month_entries)
        audit = json.loads(path.read_text()) if path.is_file() else {}
        if audit:
            if (audit.get("format") != AUDIT_FORMAT or audit.get("entry_dates") != month_entries
                    or audit.get("method_fingerprint") != method
                    or audit.get("counting_unit") != "entry_date_x_etf"
                    or audit.get("formal_list_sha256") != _audit_digest(audit["days"])):
                raise ValueError("正式名单、固定日历或计数规则已改变")
            for name, digest in audit["prediction_hashes"].items():
                if not Path(name).is_file() or hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                    raise ValueError("冻结正式名单的预测输入已改变")
            frozen_hashes[month] = audit["formal_list_sha256"]
        for entry in month_entries:
            item = audit.get("days", {}).get(entry, {})
            formal = [_scored_net(net, rows) for net in item.get("formal_signals", [])]
            nets = [_scored_net(net, rows) for net in item.get("all_net_signals", [])]
            formal_rows.extend(formal)
            net_rows.extend(nets)
            count = len(formal)
            complete = bool(item)
            if item and item.get("countable_signals") != count:
                raise ValueError("每日数量和正式计分名单不一致")
            coverage.append({"entry_date": entry, "signals": count,
                "bullish_signals": sum(row["direction"] == "利好" for row in formal),
                "bearish_signals": sum(row["direction"] == "利空" for row in formal),
                "all_net_signals": len(nets), "reported_events": item.get("reported_events"),
                "bearish_priority_etfs": item.get("bearish_priority_etfs", []),
                "audit_complete": complete,
                "scoring_incomplete": sum(not _is_scored(row) for row in formal)})
    metrics = direction_metrics(formal_rows)
    for side, direction in (("bullish", "利好"), ("bearish", "利空")):
        part = direction_metrics([row for row in formal_rows if row["direction"] == direction])
        metrics[side] = part
    monthly: dict[str, Any] = {}
    for month in sorted({entry[:7] for entry in entries}):
        monthly_rows = [row for row in formal_rows if row["entry_date"].startswith(month)]
        daily = [item for item in coverage if item["entry_date"].startswith(month)]
        monthly[month] = {"formal_signals": direction_metrics(monthly_rows),
            "bullish": direction_metrics([row for row in monthly_rows if row["direction"] == "利好"]),
            "bearish": direction_metrics([row for row in monthly_rows if row["direction"] == "利空"]),
            "total_signals": len(monthly_rows),
            "distinct_entry_dates": len({row["entry_date"] for row in monthly_rows}),
            "entry_days": daily,
            "zero_signal_dates": [item["entry_date"] for item in daily if item["audit_complete"] and item["signals"] == 0],
            "missing_entry_audit_dates": [item["entry_date"] for item in daily if not item["audit_complete"]]}
        monthly[month]["prediction_coverage_complete"] = bool(daily and all(item["audit_complete"] for item in daily))
    coverage_complete = bool(entries and all(item["prediction_coverage_complete"] for item in monthly.values())
        and all(day["refinement_complete"] for day in days))
    metrics.update({"prediction_coverage_complete": coverage_complete,
        "countable_signals": len(formal_rows), "entry_days": coverage,
        "zero_signal_dates": [item["entry_date"] for item in coverage if item["audit_complete"] and item["signals"] == 0],
        "missing_entry_audit_dates": [item["entry_date"] for item in coverage if not item["audit_complete"]]})
    mapping_rows = [row for row in rows if row.get("entry_date") in entries or row.get("entry_date") is None]
    return {"start": str(start), "end": str(end), "complete_days": len(days),
        "raw_source_news": sum(day["raw_source_news"] for day in days),
        "candidates": sum(day["candidates"] for day in days),
        "initial_batches": sum(day["batches"] for day in days), "selected": sum(day["selected"] for day in days),
        "event_etf_rows": len(rows), "formal_list_sha256": frozen_hashes,
        "directional_signals": metrics, "all_net_signals": direction_metrics(net_rows),
        "all_mapping_signals": direction_metrics(mapping_rows), "formal_results": formal_rows,
        "monthly": monthly, "days": days,
        "result_review_owner": "main_agent", "review_status": "pending",
        "independence_note": "historical holdout may be in model pretraining; correlated ETF forecasts are not independent observations; distinct from timestamped pre-open prospective validation"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    fixed = json.loads(args.plan.read_text())
    entries = [entry for month in fixed["entry_dates"].values() for entry in month
               if args.start <= date.fromisoformat(entry) <= args.end]
    result = score(args.input_root, args.start, args.end, entry_dates=entries)
    output = args.input_root / "reports" / "period" / f"{args.start}_{args.end}.json"
    _atomic_text(output, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(output), "review_status": result["review_status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
