from __future__ import annotations

import json
from datetime import date
from typing import Any

from ..settings import Settings
from .prepare import _atomic_text


DIRECTIONS = {"利好", "利空"}
RESULT_FIELDS = {"event_id", "record_ids", "summary", "etf_code", "direction", "reason"}


def day_records(settings: Settings, day: date) -> dict[str, dict[str, Any]]:
    path = settings.output_root / "days" / day.isoformat() / "records.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"尚未准备新闻: {path}")
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            for record_id in record["record_ids"]:
                result[record_id] = record
    return result


def validate_initial_results(settings: Settings, day: date) -> dict[str, int]:
    """Validate optional per-batch IDs-only JSON files against prepared order."""
    manifest = json.loads((settings.output_root / "days" / day.isoformat() / "manifest.json").read_text(encoding="utf-8"))
    selected_total = 0
    complete = 0
    latest_initial_time = 0
    batch_states: dict[str, str] = {}
    for batch in manifest["batches"]:
        result_path = settings.output_root / "results" / "initial" / day.isoformat() / batch["file"].replace(".txt", ".json")
        if not result_path.exists():
            batch_states[batch["file"]] = "pending"
            continue
        batch_path = settings.output_root / "days" / day.isoformat() / batch["file"]
        if result_path.stat().st_mtime_ns < batch_path.stat().st_mtime_ns:
            raise ValueError(f"{result_path}: 初筛结果过期，输入批次已更新")
        value = json.loads(result_path.read_text(encoding="utf-8"))
        selected = value.get("selected_ids") if isinstance(value, dict) and set(value) == {"selected_ids", "rejected"} else None
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise ValueError(f"{result_path}: selected_ids 必须是字符串列表")
        from ..baseline import _checked_initial_ids
        _checked_initial_ids(value, batch)
        order = {record_id: index for index, record_id in enumerate(batch["ids"])}
        if any(item not in order for item in selected):
            raise ValueError(f"{result_path}: 包含非本批次记录 ID")
        positions = [order[item] for item in selected]
        if positions != sorted(set(positions)):
            raise ValueError(f"{result_path}: selected_ids 必须按输入顺序且不重复")
        selected_total += len(selected)
        complete += 1
        latest_initial_time = max(latest_initial_time, result_path.stat().st_mtime_ns)
        batch_states[batch["file"]] = "complete"
    coverage = {"complete_batches": complete, "total_batches": len(manifest["batches"]), "selected_ids": selected_total}
    coverage_path = settings.output_root / "days" / day.isoformat() / "coverage.json"
    previous = json.loads(coverage_path.read_text(encoding="utf-8")) if coverage_path.exists() else {}
    refined_path = settings.output_root / "results" / f"{day.isoformat()}.jsonl"
    refined_state = "empty" if not manifest["batches"] else "pending"
    if refined_path.exists() and manifest["batches"]:
        refined_time = refined_path.stat().st_mtime_ns
        refined_state = ("complete" if previous.get("refined_result_state") == "complete"
                         and previous.get("refined_result_mtime_ns") == refined_time
                         and previous.get("complete_batches") == complete
                         and previous.get("selected_ids") == selected_total
                         and refined_time >= latest_initial_time
                         else "present_unvalidated")
    status = {**coverage, "batch_states": batch_states,
              "initial_result_state": "empty" if not manifest["batches"] else "complete" if complete == len(manifest["batches"]) else "pending",
              "refined_result_state": refined_state}
    if refined_state == "complete":
        status["refined_result_mtime_ns"] = previous["refined_result_mtime_ns"]
        status["event_etf_rows"] = previous.get("event_etf_rows", 0)
    _atomic_text(coverage_path, json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return coverage


def read_day_results(
    settings: Settings,
    day: date,
    universe: dict[str, str],
    *,
    records: dict[str, dict[str, Any]] | None = None,
    require_complete_initial: bool = False,
    mark_complete: bool = True,
) -> list[dict[str, Any]]:
    path = settings.output_root / "results" / f"{day.isoformat()}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"缺少 Agent 精筛结果: {path}")
    if records is None:
        records = day_records(settings, day)
    initial = validate_initial_results(settings, day)
    if require_complete_initial and initial["complete_batches"] != initial["total_batches"]:
        raise ValueError(f"{day}: 初筛尚未完成全部批次")
    selected: set[str] | None = None
    if initial["complete_batches"] == initial["total_batches"] and initial["total_batches"]:
        selected = set()
        latest_initial_time = 0
        for batch in json.loads((settings.output_root / "days" / day.isoformat() / "manifest.json").read_text(encoding="utf-8"))["batches"]:
            first_path = settings.output_root / "results" / "initial" / day.isoformat() / batch["file"].replace(".txt", ".json")
            selected.update(json.loads(first_path.read_text(encoding="utf-8"))["selected_ids"])
            latest_initial_time = max(latest_initial_time, first_path.stat().st_mtime_ns)
        if path.stat().st_mtime_ns < latest_initial_time:
            raise ValueError(f"{path}: 精筛结果过期，初筛结果已更新")
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            location = f"{path.name}:{line_number}"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{location}: JSON 无效: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{location}: 每行必须是对象")
            if set(row) != RESULT_FIELDS:
                raise ValueError(f"{location}: 精筛结果必须恰有六个业务字段")
            for key in ("event_id", "summary", "etf_code", "direction", "reason"):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValueError(f"{location}: 缺少或无效字段 {key}")
            ids = row.get("record_ids")
            if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in records for item in ids):
                raise ValueError(f"{location}: record_ids 缺失或包含未知新闻 ID")
            if len(ids) != len(set(ids)):
                raise ValueError(f"{location}: record_ids 重复")
            if selected is not None and any(item not in selected and records[item]["record_id"] not in selected for item in ids):
                raise ValueError(f"{location}: 精筛使用了初筛未入选的新闻 ID")
            if row["etf_code"] not in universe:
                raise ValueError(f"{location}: ETF 未配置: {row['etf_code']}")
            if row["direction"] not in DIRECTIONS:
                raise ValueError(f"{location}: 无效方向: {row['direction']}")
            event_key = row["event_id"], row["etf_code"]
            if event_key in seen:
                raise ValueError(f"{location}: 重复事件—ETF: {event_key}")
            seen.add(event_key)
            fields = ("event_id", "record_ids", "summary", "etf_code", "direction", "reason")
            events.append({field: row[field] for field in fields})
    if mark_complete:
        mark_refined_complete(settings, day, len(events))
    return events


def mark_refined_complete(settings: Settings, day: date, event_rows: int) -> None:
    """Publish completion only after the validated result and run metadata are saved."""
    path = settings.output_root / "results" / f"{day.isoformat()}.jsonl"
    coverage_path = settings.output_root / "days" / day.isoformat() / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage.update({"refined_result_state": "complete", "refined_result_mtime_ns": path.stat().st_mtime_ns,
                     "event_etf_rows": event_rows})
    _atomic_text(coverage_path, json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")
