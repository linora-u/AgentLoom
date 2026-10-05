"""Replay configured months and report results for the main Agent to review."""

from __future__ import annotations

import argparse
from calendar import monthrange
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from typing import Any

from .. import baseline
from ..processing.prepare import _atomic_text, prepare_day
from ..run import _write_candidates
from ..settings import load_baseline_settings, load_settings
from .baseline import _verify_prediction, prediction_contract, verify_evaluation_artifacts
from .evaluate import PriceStore, _date
from .period import score, build_entry_audit, dates


APP = Path(__file__).resolve().parents[1]
PROJECT = APP.parents[1]


def month_days(month: str) -> list[date]:
    year, number = map(int, month.split("-"))
    if f"{year:04d}-{number:02d}" != month:
        raise ValueError("月份必须为 YYYY-MM")
    return [date(year, number, day) for day in range(1, monthrange(year, number)[1] + 1)]


def _json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _version_root() -> Path:
    settings = load_baseline_settings()
    if not settings.version or not settings.development_month or not settings.validation_months:
        raise ValueError("请先配置 baseline.replay 的版本、开发月份和连续验收月份")
    return settings.evaluation_root / settings.version


def month_root(month: str) -> Path:
    return _version_root() / month


def plan() -> dict:
    """Record the calendar and scoring definition before reading results."""
    settings = load_baseline_settings()
    if settings.hold_days != 3 or settings.sell_at != "open":
        raise ValueError("正式月度验收固定为持有三个交易日、开盘卖出")
    # Read dates only. Never discard a session because its price/volume is bad.
    import pyarrow.parquet as pq
    reference = settings.prices / "ts_code=510300.SH" / "data.parquet"
    calendar = sorted({_date(item) for item in
                       pq.ParquetFile(reference).read(columns=["trade_date"])["trade_date"].to_pylist()})
    months = [settings.development_month, *settings.validation_months]
    entry_dates = {month: [str(day) for day in calendar if str(day).startswith(month)]
                   for month in months if month is not None}
    if any(not days for days in entry_dates.values()):
        raise ValueError("事前日历缺少整月交易日")
    value = {"version": settings.version, "development_month": settings.development_month,
             "validation_months": list(settings.validation_months),
             "entry_dates": entry_dates,
             "calendar_source": str(reference.resolve()),
             "calendar_dates_sha256": hashlib.sha256(json.dumps([str(d) for d in calendar]).encode()).hexdigest(),
             "decision_pipeline": "initial_once_then_refined_final",
             "direction_conflict_rule": "bearish_priority_without_agent_review",
             "evidence_review_owner": "refined_agent",
             "deduplication": "entry_date_x_etf; bearish_priority",
             "counting_unit": "entry_date_x_etf",
             "result_review_owner": "main_agent",
             "hold_days": settings.hold_days, "sell_at": settings.sell_at,
             "initial_max_tokens": settings.initial_max_tokens,
             "refined_max_tokens": settings.refined_max_tokens,
             "token_encoding": settings.token_encoding,
             "opening_reversals_are_diagnostics_only": True,
             "direction_signals": ["利好", "利空"],
             "return_thresholds": {key: str(value) for key, value in settings.thresholds.items()},
             "thresholds_inclusive": True,
             "news_window": "previous_trading_day_00:00:00_to_day_before_entry_23:59:59_Asia/Shanghai",
             "daily_evidence_cutoff": "news_day_T23:59:59+08:00",
             "missing_price_rule": "retain_in_denominator_and_fail_scoring_completeness",
             "stop_rule": "run_all_configured_months",
             "bullish_cost_scenarios_per_side_bps": [0, 10, 30],
             "prospective_validation": "separate_pre_open_timestamped_outputs_required"}
    path = _version_root() / "plan.json"
    if path.is_file() and json.loads(path.read_text()) != value:
        raise ValueError("事前计划已改变；请使用新版本并重新选择未使用的验收月份")
    if not path.is_file():
        _json(path, value)
    return value


def replay_days(month: str) -> list[date]:
    """Include the predecessor session's news for the first monthly entry."""
    import pyarrow.parquet as pq
    config = load_baseline_settings()
    calendar = sorted({_date(item) for item in pq.ParquetFile(
        config.prices / "ts_code=510300.SH" / "data.parquet").read(
        columns=["trade_date"])["trade_date"].to_pylist()})
    first = date.fromisoformat(plan()["entry_dates"][month][0])
    index = calendar.index(first)
    if index == 0:
        raise ValueError(f"{month}: 缺少首个入场日的前一交易日")
    return list(dates(calendar[index - 1], month_days(month)[-1]))


def prepare_month(month: str) -> dict:
    from .prepare_etf_exposure import exposure_as_of
    settings = load_settings()
    baseline_settings = load_baseline_settings()
    _write_candidates(replace(settings, output_root=baseline_settings.evaluation_root))
    target = month_root(month)
    universe = baseline.read_candidates(baseline_settings.candidates)
    counts: dict[str, Any] = {"month": month, "days": 0, "raw_news": 0, "prepared_news": 0,
              "initial_batches": 0, "initial_max_tokens": settings.initial_max_tokens,
              "refined_max_tokens": baseline_settings.refined_max_tokens,
              "token_encoding": settings.token_encoding}
    for day in replay_days(month):
        manifest = prepare_day(replace(settings, output_root=target), day)
        available = baseline.historical_candidates(universe, baseline_settings.prices, day)
        folder = target / "days" / str(day)
        _json(folder / "etf-universe-asof.json", {
            "evidence_cutoff": f"{day}T23:59:59+08:00", "candidates": available,
            "scope": "prespecified candidate pool with historical trading records",
            "availability_basis": "observed_trade_dates; not independently verified official listing dates",
            "candidate_names_basis": "configured labels; fact/exposure must be independently verified"})
        if baseline_settings.exposure_root is not None:
            _json(folder / "etf-exposure-asof.json", {
                "evidence_cutoff": f"{day}T23:59:59+08:00",
                "funds": {code: exposure_as_of(baseline_settings.exposure_root, code, day) for code in available},
                "source_manifest_sha256": baseline._sha256(baseline_settings.exposure_root / "manifest.json")})
        counts["days"] += 1
        counts["raw_news"] += manifest["raw_count"]
        counts["prepared_news"] += manifest["prepared_count"]
        counts["initial_batches"] += len(manifest["batches"])
        print(json.dumps({"stage": "prepare", "day": str(day),
                          "news": manifest["prepared_count"],
                          "batches": len(manifest["batches"])}, ensure_ascii=False), flush=True)
    _json(target / "preparation.json", counts)
    return counts


def _usage(month: str, role: str | None = None) -> list[dict]:
    path = load_baseline_settings().evaluation_root / "month_usage.json"
    entries = json.loads(path.read_text()) if path.is_file() else []
    if role is not None:
        entry = {"month": month, "role": role, "version": load_baseline_settings().version}
        if entry not in entries:
            entries.append(entry)
            _json(path, entries)
    return [entry for entry in entries if entry["month"] == month]


def audit_month_usage() -> dict:
    """Backfill use from artifact paths, without reading holdout outcomes."""
    settings = load_baseline_settings()
    roots = [settings.evaluation_root, APP / "evaluation" / "data"]
    evidence: dict[str, list[str]] = {}
    import re
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or not {"results", "reports", "logs"}.intersection(path.parts):
                continue
            match = re.match(r"(\d{4}-(?:0[1-9]|1[0-2]))-\d{2}", path.name)
            if match:
                evidence.setdefault(match[1], []).append(str(path.resolve()))
    path = settings.evaluation_root / "month_usage.json"
    entries = json.loads(path.read_text()) if path.is_file() else []
    for month, paths in sorted(evidence.items()):
        if not any(entry["month"] == month for entry in entries):
            entries.append({"month": month, "role": "prior_analysis",
                            "version": "historical_artifact_audit", "evidence": sorted(paths)})
    _json(path, entries)
    result = {"method": "artifact_names_only_no_holdout_returns_read",
              "roots": [str(root) for root in roots], "used_months": sorted({entry["month"] for entry in entries}),
              "planned_holdout_usage": {month: _usage(month) for month in settings.validation_months}}
    _json(settings.evaluation_root / "month_usage_audit.json", result)
    return result


def _run_day(root: Path, day: date) -> None:
    config = load_baseline_settings()
    state = baseline._settings(root, config.prices, config.candidates, day)
    expected = prediction_contract(config.hold_days, config.sell_at, config.thresholds)
    try:
        _verify_prediction(state, day, expected)
        print(json.dumps({"day": str(day), "stage": "cached"}), flush=True)
        return
    except (FileNotFoundError, ValueError):
        pass
    log = root / "logs" / f"{day}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(PROJECT / "applications") + os.pathsep + str(PROJECT / "src")}
    command = [sys.executable, "-m", "news_agent.baseline", "run", "--predict-only", "--day", str(day),
               "--input-root", str(root)]
    with log.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=PROJECT, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=14400)
    if result.returncode:
        raise RuntimeError(f"{day}: 日回放失败，已完成结果可续跑；日志 {log}")
    _verify_prediction(state, day, expected)


def _run_month(month: str, *, development: bool) -> dict:
    _usage(month, "development" if development else "validation")
    root = month_root(month)
    fixed = plan()
    for day in replay_days(month):
        if str(day)[:7] != month:
            _usage(str(day)[:7], "development_boundary" if development else "validation_boundary")
        if not development:
            check_freeze()
        _run_day(root, day)
    build_entry_audit(root, replay_days(month)[0], month_days(month)[-1], fixed["entry_dates"][month])
    for day in replay_days(month):
        if not development:
            check_freeze()
        log = root / "logs" / f"{day}.log"
        command = [sys.executable, "-m", "news_agent.baseline", "evaluate", "--day", str(day),
                   "--input-root", str(root)]
        env = {**os.environ, "PYTHONPATH": str(PROJECT / "applications") + os.pathsep + str(PROJECT / "src")}
        with log.open("a", encoding="utf-8") as stream:
            completed = subprocess.run(command, cwd=PROJECT, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, timeout=900)
            if completed.returncode:
                raise RuntimeError(f"{day}: 完整预测名单已冻结，计分失败；日志 {log}")
        print(json.dumps({"day": str(day), "stage": "scored_after_formal_list_freeze"}), flush=True)
    result = score(root, replay_days(month)[0], month_days(month)[-1],
                   entry_dates=fixed["entry_dates"][month])
    _json(root / "reports" / "period.json", result)
    timings: dict[str, dict[str, Any]] = {}
    for log in sorted((root / "logs").glob("*.log")):
        for line in log.read_text(errors="replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("stage") not in {
                    "worker_timing", "worker_cached", "initial_rejected"}:
                continue
            workflow = "initial.yaml" if item["stage"] == "initial_rejected" else item["workflow"]
            part = timings.setdefault(workflow, dict.fromkeys(
                ("calls", "seconds", "input_chars", "provider_requests", "retries",
                 "input_tokens", "output_tokens", "usage_responses", "missing_usage_calls",
                 "unreported_provider_requests", "output_rejections", "native_web_calls",
                 "cached_input_tokens", "failed_calls", "reference_read_calls", "cache_hits"), 0))
            if item["stage"] == "initial_rejected":
                part["output_rejections"] += 1
                continue
            if item["stage"] == "worker_cached":
                part["cache_hits"] += 1
                continue
            # Meter every attempt from retained evidence, including failed
            # development calls whose SDK metadata used zero placeholders.
            from agentloom.execution.observability import inspect_run
            with inspect_run(SimpleNamespace(trace_dir=Path(item["trace_dir"]), run_id=item["run_id"])) as trace:
                item.update(baseline._measured_tokens(trace.events()))
            part["calls"] += 1
            for key in ("seconds", "input_chars", "provider_requests", "retries",
                        "input_tokens", "output_tokens", "usage_responses", "unreported_provider_requests",
                        "native_web_calls", "cached_input_tokens"):
                part[key] += item.get(key) or 0
            part["missing_usage_calls"] += not item["token_usage_complete"]
            part["failed_calls"] += item.get("status") != "completed"
            part["reference_read_calls"] += len(item.get("reference_reads", []))
    for part in timings.values():
        part["token_usage_complete"] = (part["missing_usage_calls"] == 0
                                         and part["usage_responses"] == part["provider_requests"])
        if not part["usage_responses"]:
            part["input_tokens"] = part["output_tokens"] = None
    _json(root / "reports" / "performance.json", timings)
    return result


def _snapshot_paths() -> list[Path]:
    config = load_baseline_settings()
    paths: list[Path] = []
    for folder in ("config", "agent", "workflows", "processing", "evaluation", "skills"):
        paths.extend(path for path in (APP / folder).rglob("*")
                     if path.is_file() and path.suffix in {".py", ".yaml", ".md"}
                     and "data" not in path.relative_to(APP).parts and "__pycache__" not in path.parts)
    paths.extend(APP / name for name in ("baseline.py", "settings.py", "run.py", "skill_reference.py"))
    paths.extend((config.candidates, PROJECT / "config" / "system.yaml",
                  PROJECT / "src" / "runtimes" / "pi" / "protocol_handlers.py"))
    paths.extend(path for path in (PROJECT / "src").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".ts", ".json"})
    paths.extend((PROJECT / "config").glob("*.yaml"))
    paths.extend(path for path in (PROJECT / "uv.lock", PROJECT / "pyproject.toml") if path.is_file())
    bridge = PROJECT / ".venv" / "share" / "pi" / "bridge"
    paths.extend(path for folder in (bridge, bridge / "dist") for path in folder.glob("*")
                 if path.is_file() and path.suffix in {".ts", ".js", ".json"})
    for root in (config.prices, config.exposure_root):
        if root is not None:
            paths.extend(path for path in root.rglob("*") if path.is_file())
    months = [config.development_month, *config.validation_months]
    for month in months:
        if month is None:
            continue
        root = month_root(month)
        paths.extend(path for path in (root / "days").rglob("*")
                     if path.is_file() and path.name != "coverage.json")
        if month == config.development_month:
            for folder in ("results", "reports"):
                paths.extend(path for path in (root / folder).rglob("*") if path.is_file())
    return sorted(set(paths))


def freeze() -> dict:
    fixed = plan()
    config = load_baseline_settings()
    marker = _version_root() / "freeze.json"
    if marker.exists():
        raise ValueError("版本已经冻结；请直接继续独立验收")
    dev_days = replay_days(fixed["development_month"])
    dev = score(month_root(fixed["development_month"]), dev_days[0], dev_days[-1],
                entry_dates=fixed["entry_dates"][fixed["development_month"]])
    audit_month_usage()
    for month in fixed["validation_months"]:
        old_results = (any(config.evaluation_root.glob(f"**/results/{month}-*.jsonl"))
                       or any(config.evaluation_root.glob(f"**/reports/baseline/{month}-*.json")))
        if _usage(month) or old_results or (month_root(month) / "results").exists():
            raise ValueError(f"{month}: 已用于开发或验收，不能重新作为独立验收月")
    calendar = PriceStore(config.prices).calendar
    last_entry = next(day for day in calendar if day > dev_days[-1])
    exit_day = calendar[calendar.index(last_entry) + config.hold_days]
    first_validation = month_days(fixed["validation_months"][0])[0]
    if first_validation <= exit_day:
        raise ValueError("开发与验收价格窗口重叠；请调整事前计划，留出间隔")
    for month in fixed["validation_months"]:
        prepare_month(month)
    paths = {str(path.resolve()): baseline._sha256(path) for path in _snapshot_paths()}
    frozen = {"plan": fixed, "development_directions": dev["directional_signals"], "paths": paths}
    _json(marker, frozen)
    return frozen


def check_freeze() -> dict:
    path = _version_root() / "freeze.json"
    if not path.is_file():
        raise ValueError("请在读取验收结果前冻结版本和计分口径")
    value = json.loads(path.read_text())
    if value["plan"] != plan():
        raise ValueError("冻结后的验收计划已改变")
    if set(value["paths"]) != {str(path.resolve()) for path in _snapshot_paths()}:
        raise ValueError("冻结后的代码或输入文件集合发生变化")
    for name, digest in value["paths"].items():
        if not Path(name).is_file() or baseline._sha256(Path(name)) != digest:
            raise ValueError(f"冻结后的代码或输入发生变化：{name}")
    return value


def validate() -> dict:
    fixed = check_freeze()["plan"]
    roots: dict[str, Path] = {}
    combined: dict = {}
    for month in fixed["validation_months"]:
        _run_month(month, development=False)
        roots[month] = month_root(month)
        combined = score(roots, replay_days(next(iter(roots)))[0], month_days(month)[-1],
                         entry_dates=[day for key in roots for day in fixed["entry_dates"][key]])
        metrics = combined["directional_signals"]
        combined["validation_months"] = list(roots)
        combined["execution_complete"] = len(roots) == len(fixed["validation_months"])
        _json(_version_root() / "validation.json", combined)
        print(json.dumps({"stage": "validation", "months": list(roots),
                          "review_status": combined["review_status"],
                          "directions": metrics}, ensure_ascii=False), flush=True)
    return combined


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "dev", "freeze", "validate"))
    args = parser.parse_args()
    audit_month_usage()
    fixed = plan()
    if args.phase == "prepare":
        result = prepare_month(fixed["development_month"])
    elif args.phase == "dev":
        prepare_month(fixed["development_month"])
        result = _run_month(fixed["development_month"], development=True)
    elif args.phase == "freeze":
        result = freeze()
        result = {"frozen": True, "plan": result["plan"]}
    else:
        result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
