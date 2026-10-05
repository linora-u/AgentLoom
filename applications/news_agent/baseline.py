"""Historical news analysis and ETF outcome evaluation entry point."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .settings import (BaselineSettings, LEVEL_THRESHOLD_KEYS, load_baseline_settings,
                       validate_baseline_prediction)


ROOT = Path(__file__).resolve().parent
REQUIRED_PRICE_FIELDS = {"trade_date", "open", "close", "vol", "adj_factor"}
ETF_CODE = re.compile(r"\d{6}\.(?:SH|SZ)\Z")


def read_candidates(path: Path) -> dict[str, str]:
    universe: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        code, name = line.split("\t", 1)
        if not ETF_CODE.fullmatch(code) or not name.strip() or code in universe:
            raise ValueError(f"无效或重复的 ETF 候选: {code}")
        universe[code] = name.strip()
    if not universe:
        raise ValueError(f"ETF 候选池为空: {path}")
    return universe


def historical_candidates(universe: dict[str, str], prices: Path, day: date) -> dict[str, str]:
    """Pass only funds with observed trading by the news cutoff; no prices enter the Agent."""
    from .evaluation.evaluate import _date

    available = {}
    for code, name in universe.items():
        path = prices / f"ts_code={code}" / "data.parquet"
        sessions = pq.ParquetFile(path).read(columns=["trade_date"]).column(0).to_pylist()
        if any(_date(session) <= day for session in sessions):
            available[code] = name
    return available


def _entry_news_window(day: date, prices: Path) -> tuple[date, date, date]:
    """Resolve the prespecified entry window using trading dates only."""
    from .evaluation.evaluate import _date

    calendar = sorted({_date(value) for value in pq.ParquetFile(
        prices / "ts_code=510300.SH" / "data.parquet").read(
        columns=["trade_date"])["trade_date"].to_pylist()})
    index = next((i for i, session in enumerate(calendar) if session > day), None)
    if index is None or index == 0:
        raise ValueError(f"{day}: 日历缺少下一入场日或其前一交易日")
    entry = calendar[index]
    return calendar[index - 1], entry - timedelta(days=1), entry


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".copy-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copyfile(source, temporary)
        if _sha256(source) != _sha256(temporary):
            raise ValueError(f"ETF 行情复制后校验失败: {source}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".json-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".text-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def copy_prices(source: Path, target: Path, candidates: Path) -> dict:
    universe = read_candidates(candidates)
    files: dict[str, Path] = {}
    for code in universe:
        path = source / f"ts_code={code}" / "data.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"ETF 行情缺失: {path}")
        try:
            parquet = pq.ParquetFile(path)
            missing = REQUIRED_PRICE_FIELDS - set(parquet.schema_arrow.names)
            if missing:
                raise ValueError(f"ETF 行情字段缺失 {code}: {sorted(missing)}")
            for _ in parquet.iter_batches(batch_size=8192, columns=sorted(REQUIRED_PRICE_FIELDS)):
                pass
        except (OSError, pa.ArrowException) as error:
            raise ValueError(f"ETF 行情数据无法读取 {code}: {error}") from error
        files[code] = path

    marker = target / "copy_manifest.json"
    marker.unlink(missing_ok=True)
    copied: dict[str, dict] = {}
    for code, path in files.items():
        destination = target / f"ts_code={code}" / "data.parquet"
        source_digest = _sha256(path)
        if not destination.is_file() or _sha256(destination) != source_digest:
            _atomic_copy(path, destination)
        if _sha256(destination) != source_digest:
            raise ValueError(f"ETF 行情复制后校验失败: {destination}")
        copied[code] = {"sha256": source_digest, "bytes": destination.stat().st_size}
    manifest = {
        "source_root": str(source.resolve()),
        "copied_at": datetime.now(timezone.utc).isoformat(),
        "files": copied,
    }
    _atomic_json(marker, manifest)
    return manifest


def verify_local_prices(root: Path, universe: dict[str, str]) -> dict:
    marker = root / "copy_manifest.json"
    if not marker.is_file():
        raise FileNotFoundError(f"本地 ETF 行情副本未准备完整，请先运行 copy-prices: {marker}")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    if set(manifest.get("files", {})) != set(universe):
        raise ValueError("本地 ETF 行情副本与候选池不一致")
    for code, recorded in manifest["files"].items():
        path = root / f"ts_code={code}" / "data.parquet"
        if not path.is_file() or _sha256(path) != recorded["sha256"]:
            raise ValueError(f"本地 ETF 行情副本缺失或发生变化: {code}")
    return manifest


def _model_signature() -> str:
    raw = yaml.safe_load((ROOT / "config" / "model.yaml").read_text(encoding="utf-8"))
    profiles = {}
    for name, search in (("codex_luna_news_initial", "off"),
                         ("codex_luna_news_replay", "auto")):
        profile = raw["model"][name]
        if (profile.get("adapter") != "openai_codex_responses"
                or profile.get("web_search") != search):
            raise ValueError(f"{name}: Pi Codex 模型及联网设置与当前流程不一致")
        profiles[name] = profile
    digest = hashlib.sha256(json.dumps(profiles, sort_keys=True).encode())
    paths = [path for folder in ("agent", "skills", "config", "workflows", "processing", "evaluation")
             for path in (ROOT / folder).rglob("*") if path.is_file()
             and path.suffix in {".py", ".md", ".yaml"}
             and not {"data", "prehistory", "__pycache__"}.intersection(path.relative_to(ROOT).parts)]
    paths.extend(ROOT / name for name in ("baseline.py", "settings.py", "run.py", "report.py", "skill_reference.py"))
    project = ROOT.parents[1]
    paths.extend((project / "config").glob("*.yaml"))
    paths.extend(path for path in (project / "src").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".ts", ".json"})
    bridge = project / ".venv" / "share" / "pi" / "bridge"
    paths.extend(path for folder in (bridge, bridge / "dist") for path in folder.glob("*")
                 if path.is_file() and path.suffix in {".ts", ".js", ".json"})
    paths.extend(path for path in (project / "uv.lock", project / "pyproject.toml") if path.is_file())
    for path in sorted(set(paths)):
        digest.update(str(path.resolve()).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _stage_signature(stage: str) -> str:
    """Keep unrelated prompts and reporting changes out of model caches."""
    model = "codex_luna_news_initial" if stage == "initial" else "codex_luna_news_replay"
    profile = yaml.safe_load((ROOT / "config" / "model.yaml").read_text())["model"][model]
    prompt = "初筛.md" if stage == "initial" else "精筛.md"
    paths = [ROOT / "agent" / prompt, ROOT / "workflows" / f"{stage}.yaml",
             ROOT / "config" / "system.yaml"]
    if stage == "refined":
        paths.extend(path for path in (ROOT / "skills" / "news-catalyst").rglob("*") if path.is_file())
        paths.extend((ROOT / "skill_reference.py", ROOT / "baseline.py"))
    project = ROOT.parents[1]
    paths.extend((project / "config").glob("*.yaml"))
    paths.extend(path for path in (project / "src" / "runtimes" / "pi").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".ts", ".json"})
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode())
    if stage == "initial":
        import inspect
        for function in (_classify_initial, _initial_classifications, _initial_rows,
                         _news_query, IncompleteNewsResults, _checked_initial_ids, _indexed_news):
            digest.update(inspect.getsource(function).encode())
    for path in sorted(set(paths)):
        digest.update(str(path.resolve()).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _request_fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _skill_signature() -> str:
    """Invalidate refined results whenever the local skill instructions change."""
    directory = ROOT / "skills" / "news-catalyst"
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _measured_tokens(events: list[dict]) -> dict:
    requests = [item for item in events if item["kind"] == "model_request"
                and item.get("provider_request_complete")]
    usages = [item["usage"] for item in events if item["kind"] == "model_response"
              and item.get("usage") is not None
              and (item["usage"]["input_tokens"] > 0 or item["usage"]["output_tokens"] > 0)]
    seen_turns: set[str] = set()
    retries = 0
    for item in requests:
        turn_id = item.get("model_turn_id")
        retries += int((item.get("attempt") or 0) > 0 or (turn_id is not None and turn_id in seen_turns))
        if turn_id is not None:
            seen_turns.add(turn_id)
    return {"provider_requests": len(requests),
            "retries": retries,
            "input_tokens": sum(item["input_tokens"] for item in usages) if usages else None,
            "output_tokens": sum(item["output_tokens"] for item in usages) if usages else None,
            "usage_responses": len(usages),
            "unreported_provider_requests": len(requests) - len(usages),
            "token_usage_complete": bool(usages) and len(usages) == len(requests)}


def _call_worker(definition: Path, query: str, *, etf_codes: list[str] | None = None) -> dict:
    from agentloom.app.factory import YamlAgentFactory
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.config.llm_config import LLMConfig
    from agentloom.execution.context import RuntimeHome, bind_run_context
    from agentloom.execution.logging import NullLoggerBackend
    from agentloom.execution.observability import bind_trace_recorder, inspect_run

    project_root = ROOT.parents[1]
    config = load_project_config(project_root)
    raw_models = yaml.safe_load((project_root / "config" / "llm.yaml").read_text(encoding="utf-8"))
    local_models_raw = yaml.safe_load((ROOT / "config" / "model.yaml").read_text(encoding="utf-8"))
    raw_models["model"].update(local_models_raw["model"])
    local_models = LLMConfig.from_dict(raw_models).models
    for name in local_models_raw["model"]:
        config.llm.models[name] = local_models[name]
    context = RuntimeHome(ROOT / "data" / "runtime").new_context(application_id="news_agent")
    context.prepare_run()
    context.write_manifest(workflow=definition.name, input_chars=len(query))
    started = time.perf_counter()
    status = "failed"
    try:
        with bind_config(config), bind_run_context(context), bind_trace_recorder(context):
            worker_definition: Path | dict = definition
            worker_options: dict[str, Any] = {"logger": NullLoggerBackend()}
            if etf_codes is not None:
                prepared_definition: dict = YamlAgentFactory._load_config_from_file(definition)
                worker_definition = prepared_definition
                schema = prepared_definition["output_schema"]["properties"]
                codes = sorted(set(etf_codes))
                schema["reviews"]["items"]["properties"]["etf_code"]["enum"] = [*codes, None]
                worker_options["_source_path_is_pinned"] = True
            worker = YamlAgentFactory.create_agent_as_tool(worker_definition, **worker_options)
            if worker is None:
                raise RuntimeError(f"无法创建 Pi Worker: {definition}")
            response = worker(query=query)
        value = json.loads(response) if isinstance(response, str) else response
        if not isinstance(value, dict):
            raise ValueError(f"Pi Worker 未返回 JSON 对象: {definition.name}")
        status = "completed"
    finally:
        with inspect_run(context) as trace:
            events = trace.events()
            responses = [json.loads(trace.read_text(item["response_ref"]))
                         for item in events if item["kind"] == "model_response" and item.get("response_ref")]
            native_calls = [call for response in responses
                            for call in response.get("native_search", {}).get("calls", [])]
            original_opens = [call for call in native_calls
                              if isinstance(call.get("action"), dict)
                              and call.get("status") == "completed"
                              and call["action"].get("type") == "open_page"
                              and call.get("action_url_sha256")]
            citations = sorted({citation["url"] for response in responses
                                for citation in response.get("native_search", {}).get("citations", [])})
            reference_reads = [json.loads(trace.read_text(item["input_ref"])).get("filename")
                               for item in events if item["kind"] == "tool"
                               and item.get("tool_name") == "read_news_reference" and item.get("status") == "completed"]
            skill_loaded = any(item["kind"] == "tool" and item.get("tool_name") == "skill"
                               and item.get("status") == "completed" and item.get("input_ref")
                               and json.loads(trace.read_text(item["input_ref"])).get("name") == "news-catalyst"
                               for item in events)
        timing = {"stage": "worker_timing", "workflow": definition.name, "status": status,
                  "input_chars": len(query), "seconds": round(time.perf_counter() - started, 3),
                  **_measured_tokens(events), "run_id": context.run_id,
                  "native_web_calls": len(native_calls), "citation_urls": citations,
                  "native_open_page_calls": len(original_opens),
                  "opened_original_url_hashes": sorted({call["action_url_sha256"] for call in original_opens}),
                  "reference_reads": reference_reads,
                  "skill_loaded": skill_loaded,
                  "cached_input_tokens": sum(response.get("usage", {}).get("cacheRead", 0) for response in responses),
                  "trace_dir": str(context.trace_dir)}
        context.update_manifest(status=status, performance=timing)
        print(json.dumps(timing, ensure_ascii=False), flush=True)
    return value


def _result_is_current(path: Path, fingerprint: str) -> bool:
    meta = path.with_suffix(".meta.json")
    if not path.is_file() or not meta.is_file():
        return False
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return value.get("fingerprint") == fingerprint and value.get("sha256") == _sha256(path)


def _record_result(path: Path, fingerprint: str, value: str) -> None:
    _atomic_text(path, value)
    _atomic_json(path.with_suffix(".meta.json"), {"fingerprint": fingerprint, "sha256": _sha256(path)})


def _prepared_day(root: Path, day: date, expected_records: int | None = None,
                  expected_batches: int | None = None) -> dict:
    directory = root / "days" / day.isoformat()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    records_path = directory / "records.jsonl"
    if manifest.get("records_sha256") and _sha256(records_path) != manifest["records_sha256"]:
        raise ValueError(f"{day}: 标准化新闻摘要不符")
    for batch in manifest["batches"]:
        path = directory / batch["file"]
        if batch.get("sha256") and _sha256(path) != batch["sha256"]:
            raise ValueError(f"{day}: 新闻批次摘要不符: {batch['file']}")
    batch_ids = [item for batch in manifest["batches"] for item in batch["ids"]]
    record_ids = [json.loads(line)["record_id"] for line in records_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if (len(record_ids) != manifest["prepared_count"] or len(set(record_ids)) != len(record_ids)
            or len(batch_ids) != len(record_ids) or set(batch_ids) != set(record_ids)):
        raise ValueError(f"{day}: 新闻记录、批次 ID 与准备记录数不一致")
    if ((expected_records is not None and len(record_ids) != expected_records)
            or (expected_batches is not None and len(manifest["batches"]) != expected_batches)):
        raise ValueError(f"{day}: 输入未达到指定的 {expected_records} 条、{expected_batches} 批")
    return manifest


def _settings(root: Path, prices: Path, candidates: Path, day: date):
    from .settings import Settings

    return Settings(news_root=ROOT, etf_root=prices, etf_config=candidates,
                    output_root=root, mode="baseline",
                    day=day, year=day.year)


def _checked_events(events: object, allowed: set[str], records: dict,
                    universe: dict[str, str]) -> list[dict]:
    from .processing.results import DIRECTIONS, RESULT_FIELDS

    if not isinstance(events, list):
        raise ValueError("精筛输出的 events 必须是数组")
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for row in events:
        if not isinstance(row, dict) or set(row) != RESULT_FIELDS:
            raise ValueError("精筛事件必须恰有六个业务字段")
        if any(not isinstance(row[key], str) or not row[key].strip()
               for key in ("event_id", "summary", "etf_code", "direction", "reason")):
            raise ValueError("精筛事件有空值或无效文本")
        ids = row["record_ids"]
        if not isinstance(ids, list) or not ids or any(
            not isinstance(item, str) or item not in records or records[item]["record_id"] not in allowed
            for item in ids
        ) or len(ids) != len(set(ids)):
            raise ValueError("精筛事件包含未入选或重复的新闻 ID")
        if row["etf_code"] not in universe:
            raise ValueError(f"精筛事件包含未配置 ETF: {row['etf_code']}")
        if row["direction"] not in DIRECTIONS:
            raise ValueError("精筛只能输出利好或利空交易信号")
        key = (row["event_id"], row["etf_code"])
        if key in seen:
            raise ValueError("精筛事件与 ETF 重复")
        seen.add(key)
        result.append(row)
    return result


def _checked_initial_ids(answer: dict, batch: dict) -> list[str]:
    ids = answer.get("selected_ids") if set(answer) == {"selected_ids", "rejected"} else None
    positions = {record_id: index for index, record_id in enumerate(batch["ids"])}
    if not isinstance(ids, list) or any(
            not isinstance(item, str) or item not in positions for item in ids):
        unknown = [item for item in ids if not isinstance(item, str) or item not in positions] if isinstance(ids, list) else []
        raise ValueError(f"{batch['file']}: 初筛输出包含非本批次 ID 或缺少 selected_ids: {unknown[:5]}")
    if len(ids) != len(set(ids)):
        raise ValueError("初筛入选 ID 重复")
    rejected = answer["rejected"]
    if not isinstance(rejected, list):
        raise ValueError("初筛 rejected 必须是分类数组")
    covered = set(ids)
    for group in rejected:
        if (not isinstance(group, dict) or set(group) != {"reason_code", "record_ids"}
                or group["reason_code"] not in {"观点复盘", "行情复述", "旧闻无增量", "单股影响有限", "无ETF传导", "非财经", "输入不可核", "其他"}
                or not isinstance(group["record_ids"], list) or not group["record_ids"]
                or any(not isinstance(item, str) or item not in positions or item in covered for item in group["record_ids"])
                or len(group["record_ids"]) != len(set(group["record_ids"]))):
            raise ValueError("初筛剔除原因缺失、重复或包含未知 ID")
        covered.update(group["record_ids"])
    if covered != set(positions):
        raise ValueError("初筛未逐条保留全部入选及放弃原因")
    return sorted(set(ids), key=positions.__getitem__)


INITIAL_CLASSIFICATIONS = ("入选", "观点复盘", "行情复述", "旧闻无增量", "单股影响有限",
                           "无ETF传导", "非财经", "输入不可核", "其他")


def _indexed_news(body: str, ids: list[str]) -> str:
    """Replace primary record headers only; the full body and provenance remain."""
    for index, record_id in enumerate(ids, 1):
        header = f"记录ID: {record_id}\n"
        if body.count(header) != 1:
            raise ValueError(f"{record_id}: 本批次主记录边界缺失或重复")
        body = body.replace(header, f"记录序号: {index}\n", 1)
    return body


class IncompleteNewsResults(ValueError):
    """Keep partial results for the main Agent; never split and rerun completed news."""


def _news_query(context: str, ids: list[str], records: dict,
                exposure_by_id: dict[str, list[dict]]) -> str:
    from .processing.prepare import _record_input

    clues = [{"record_number": index, "reported_holdings": exposure_by_id[record_id]}
             for index, record_id in enumerate(ids, 1) if exposure_by_id.get(record_id)]
    evidence = (("新闻日前已公告的 ETF 持仓线索（不代表基金完整持仓，也不是买入证明）：\n"
                 + json.dumps(clues, ensure_ascii=False, separators=(",", ":")) + "\n") if clues else "")
    body = "".join(_record_input(records[record_id]) for record_id in ids)
    return (context + f"\n本次提供 {len(ids)} 条新闻，记录序号从 1 至 {len(ids)}；"
            "仅处理本次给出的完整新闻，返回全部对应分类或审核结论。\n\n"
            + evidence + _indexed_news(body, ids))


def _initial_rows(answer: dict, ids: list[str]) -> dict[int, int]:
    rows = answer.get("classifications") if set(answer) == {"classifications"} else None
    if not isinstance(rows, list):
        raise ValueError("初筛 classifications 必须是数组")
    codes: dict[int, int] = {}
    previous = 0
    for row in rows:
        if (not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int
                or not previous < row[0] <= len(ids) or type(row[1]) is not int
                or not 0 <= row[1] < len(INITIAL_CLASSIFICATIONS)):
            raise ValueError("初筛必须使用本批次不重复的 [记录序号, 有效分类代码]")
        codes[row[0]] = row[1]
        previous = row[0]
    return codes


def _initial_classifications(answer: dict, batch: dict) -> dict:
    """Restore exact IDs from a complete classification ledger."""
    codes = _initial_rows(answer, batch["ids"])
    if len(codes) != len(batch["ids"]):
        raise IncompleteNewsResults(f"{batch['file']}: classifications 必须完整覆盖 {len(batch['ids'])} 条新闻")
    selected: list[str] = []
    rejected: dict[str, list[str]] = {}
    for index, record_id in enumerate(batch["ids"], 1):
        code = codes[index]
        if code == 0:
            selected.append(record_id)
        else:
            rejected.setdefault(INITIAL_CLASSIFICATIONS[code], []).append(record_id)
    result = {"selected_ids": selected, "rejected": [
        {"reason_code": reason, "record_ids": ids} for reason, ids in rejected.items()]}
    _checked_initial_ids(result, batch)
    return result


def _classify_initial(query: str, batch: dict, records: dict, path: Path,
                      fingerprint: str, context: str) -> dict:
    partial = path.with_name(path.stem + "-partial.json")
    if _result_is_current(partial, fingerprint):
        answer = json.loads(partial.read_text(encoding="utf-8"))
    else:
        answer = _call_worker(ROOT / "workflows" / "initial.yaml", query)
    codes = _initial_rows(answer, batch["ids"])
    missing_numbers = [index for index in range(1, len(batch["ids"]) + 1) if index not in codes]
    if missing_numbers:
        _record_result(partial, fingerprint, json.dumps({"classifications": sorted(codes.items())}, ensure_ascii=False) + "\n")
        missing = [batch["ids"][index - 1] for index in missing_numbers]
        print(json.dumps({"stage": "initial_supplement", "batch": batch["file"],
                          "retained_records": len(codes), "missing_record_ids": missing}, ensure_ascii=False), flush=True)
        try:
            supplement = _call_worker(ROOT / "workflows" / "initial.yaml",
                _news_query(context + "\n这是初筛遗漏记录的补充任务，已有分类已保存。", missing, records, {}))
            extra = _initial_rows(supplement, missing)
        except (ValueError, RuntimeError) as error:
            raise IncompleteNewsResults(f"{batch['file']}: 初筛补漏未完成，已有结果保留；{error}") from error
        codes.update({missing_numbers[index - 1]: code for index, code in extra.items()})
        answer = {"classifications": [[index, code] for index, code in sorted(codes.items())]}
        _record_result(partial, fingerprint, json.dumps(answer, ensure_ascii=False) + "\n")
        remaining = [batch["ids"][index - 1] for index in missing_numbers if index not in codes]
        if remaining:
            raise IncompleteNewsResults(f"{batch['file']}: 初筛补漏后仍遗漏新闻: {remaining}")
    result = _initial_classifications(answer, batch)
    _record_result(path, fingerprint, json.dumps(result, ensure_ascii=False) + "\n")
    for file in (partial, partial.with_suffix(".meta.json")):
        file.unlink(missing_ok=True)
    return result


def _refined_wire_result(value: dict, ids: list[str]) -> dict:
    """Restore exact news IDs from the single model review ledger."""
    if set(value) != {"reviews"} or not isinstance(value["reviews"], list):
        raise ValueError("精筛 Worker 必须只返回 reviews 数组")
    fields = {"event_id", "etf_code", "record_numbers", "summary", "decision", "reason", "sources"}
    reviews = []
    for row in value["reviews"]:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("精筛 review 必须使用 record_numbers 及完整业务字段")
        numbers = row["record_numbers"]
        if (not isinstance(numbers, list) or not numbers
                or any(type(number) is not int or not 1 <= number <= len(ids) for number in numbers)
                or len(numbers) != len(set(numbers))):
            raise ValueError(f"record_numbers 必须是本块不重复的整数序号 1 至 {len(ids)}")
        reviews.append({**{key: item for key, item in row.items() if key != "record_numbers"},
                        "record_ids": [ids[number - 1] for number in numbers]})
    return {"reviews": reviews}


def _refined_worker_rows(path: Path, fingerprint: str, definition: Path, query: str,
                         record_ids: list[str], records: dict,
                         universe: dict[str, str], context: str,
                         exposure_by_id: dict[str, list[dict]]) -> list[dict]:
    allowed = set(record_ids)
    if len(allowed) != len(record_ids):
        raise ValueError("精筛输入主记录重复")
    cached = _result_is_current(path, fingerprint)

    def validate(value: dict) -> tuple[list[dict], list[str]]:
        if set(value) != {"reviews"} or not isinstance(value["reviews"], list):
            raise ValueError("精筛 Worker 必须只返回 reviews 数组")
        events = []
        reviewed = set()
        fields = {"event_id", "etf_code", "record_ids", "summary", "decision", "reason", "sources"}
        for review in value["reviews"]:
            if not isinstance(review, dict) or set(review) != fields:
                raise ValueError("精筛 review 字段不完整")
            ids = review["record_ids"]
            if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
                    or any(not isinstance(item, str) or item not in allowed for item in ids)):
                raise ValueError("精筛 review 必须引用本块有效新闻 ID")
            if (review["decision"] not in {"利好", "利空", "放弃"}
                    or any(not isinstance(review[key], str) or not review[key].strip()
                           for key in ("summary", "reason"))
                    or not isinstance(review["sources"], list)):
                raise ValueError("精筛 review 缺少有效结论或理由")
            if review["etf_code"] is not None and review["etf_code"] not in universe:
                raise ValueError("精筛 review 引用了未配置 ETF")
            reviewed.update(ids)
            if review["decision"] != "放弃":
                events.append({"event_id": review["event_id"], "record_ids": ids,
                    "summary": review["summary"], "etf_code": review["etf_code"],
                    "direction": review["decision"], "reason": review["reason"]})
        return (_checked_events(events, allowed, records, universe),
                [record_id for record_id in record_ids if record_id not in reviewed])

    if cached:
        print(json.dumps({"stage": "worker_cached", "workflow": definition.name,
                          "cache": str(path)}, ensure_ascii=False), flush=True)
        rows, missing = validate(json.loads(path.read_text(encoding="utf-8")))
        if missing:
            raise IncompleteNewsResults(f"精筛缓存遗漏候选新闻判断及放弃原因: {missing}")
        return rows
    partial = path.with_name(path.stem + "-partial.json")
    if _result_is_current(partial, fingerprint):
        canonical = json.loads(partial.read_text(encoding="utf-8"))
    else:
        canonical = _refined_wire_result(_call_worker(definition, query, etf_codes=list(universe)), record_ids)
    rows, missing = validate(canonical)
    if missing:
        _record_result(partial, fingerprint, json.dumps(canonical, ensure_ascii=False) + "\n")
        print(json.dumps({"stage": "refined_supplement", "file": path.name,
                          "retained_records": len(allowed) - len(missing),
                          "missing_record_ids": missing}, ensure_ascii=False), flush=True)
        try:
            answer = _call_worker(definition,
                _news_query(context + "\n这是精筛遗漏记录的补充任务，已有判断及证据已保存。",
                            missing, records, exposure_by_id), etf_codes=list(universe))
            additions = _refined_wire_result(answer, missing)
            # Event IDs are local to each Agent task; retain all its ETF mappings.
            namespace = "supplement-" + _request_fingerprint(fingerprint, *missing)[:12] + "-"
            combined = {"reviews": canonical["reviews"] + [
                {**row, "event_id": namespace + row["event_id"] if row["event_id"] is not None else None}
                for row in additions["reviews"]]}
            rows, remaining = validate(combined)
        except (ValueError, RuntimeError) as error:
            raise IncompleteNewsResults(f"精筛补漏未完成，已有结果保留；{error}") from error
        canonical = combined
        _record_result(partial, fingerprint, json.dumps(canonical, ensure_ascii=False) + "\n")
        if remaining:
            raise IncompleteNewsResults(f"精筛补漏后仍遗漏候选新闻判断及放弃原因: {remaining}")
    _record_result(path, fingerprint, json.dumps(canonical, ensure_ascii=False) + "\n")
    for file in (partial, partial.with_suffix(".meta.json")):
        file.unlink(missing_ok=True)
    return rows


def _identified_refined_event(index: int, row: dict) -> dict:
    return {**row, "event_id": f"c{index:03d}-{row['event_id']}"}


def merge_etf_decisions(rows: list[dict]) -> list[dict]:
    """Apply the user rule to final refinement decisions, without another Agent."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["etf_code"], []).append(row)
    merged = []
    for code, candidates in sorted(grouped.items()):
        direction = "利空" if any(row["direction"] == "利空" for row in candidates) else "利好"
        chosen = [row for row in candidates if row["direction"] == direction]
        merged.append({"event_id": f"etf-{code}", "etf_code": code, "direction": direction,
            "record_ids": list(dict.fromkeys(item for row in chosen for item in row["record_ids"])),
            "summary": "；".join(dict.fromkeys(row["summary"] for row in chosen)),
            "reason": "；".join(dict.fromkeys(row["reason"] for row in chosen))})
    return merged


def _analyze_selected(day: date, root: Path, selected: list[str], records: dict,
                      universe: dict[str, str], args: argparse.Namespace,
                      model_signature: str, baseline_settings: BaselineSettings) -> list[dict]:
    from .processing.prepare import chunk_selected_records
    from .evaluation.prepare_etf_exposure import holding_mentions_as_of

    review_path = root / "results" / "reviews" / f"{day.isoformat()}.json"
    if not selected:
        _atomic_json(review_path, {"day": day.isoformat(), "selected_records": 0,
                                   "reviews": [], "mapping_signals": [], "final_signals": []})
        return []
    universe = historical_candidates(universe, getattr(args, "prices", baseline_settings.prices), day)
    if not universe:
        raise ValueError(f"{day}: 候选池在该历史日没有具备交易记录的 ETF")
    limits = _limits(args)
    prompt = (ROOT / "agent" / "精筛.md").read_text(encoding="utf-8")
    definition = ROOT / "workflows" / "refined.yaml"
    exposure_by_id: dict[str, list[dict]] = {}
    if baseline_settings.exposure_root is not None:
        headlines = {record_id: ((records[record_id].get("title") or "") + "\n"
                                 + (records[record_id].get("text") or ""))
                     for record_id in selected}
        evidence = holding_mentions_as_of(baseline_settings.exposure_root, day, headlines)
        exposure_by_id = {record_id: [position for position in positions
                                      if position["fund"] in universe]
                          for record_id, positions in evidence.items()}
    worker_signature = _request_fingerprint(_stage_signature("refined"), prompt, definition.read_text(encoding="utf-8"),
                                            _skill_signature(), _sha256(ROOT / "skill_reference.py"),
                                            str(baseline_settings.refined_max_tokens),
                                            baseline_settings.token_encoding)
    window_start, window_end, entry = _entry_news_window(
        day, getattr(args, "prices", baseline_settings.prices))
    base = (f"mode=historical_replay；历史日期={day.isoformat()}；"
            f"入场交易日={entry.isoformat()}；"
            f"新闻窗口={window_start.isoformat()}T00:00:00+08:00"
            f"至{window_end.isoformat()}T23:59:59+08:00；"
            f"在当天结束后的下一交易日开盘入场，经过 {args.hold_days} 个交易日，"
            f"于出场日 {args.sell_at} 观察复权收益。方向推荐的收益门槛（小数）："
            f"{json.dumps({key: str(value) for key, value in limits.items()}, ensure_ascii=False)}。"
            f"证据截止={day.isoformat()}T23:59:59+08:00。"
            "自主联网核查原始发布、首披、此前状态与反向进展；"
            "原始 URL、公开时间与决定性依据写入 reason。"
            "只采用截止前可核实的版本，不检索或推断新闻日之后的行情。\n"
            "ETF 候选（截至新闻日已有交易记录）：\n"
            + "\n".join(f"{code}\t{name}" for code, name in universe.items()) + "\n")
    parts = chunk_selected_records(selected, records, baseline_settings.refined_max_tokens,
                                   baseline_settings.token_encoding)

    def clue_context(ids: list[str]) -> str:
        exposure_evidence = [{"record_number": index,
                              "reported_holdings": exposure_by_id[record_id]}
                             for index, record_id in enumerate(ids, 1) if exposure_by_id.get(record_id)]
        return (("新闻日前已公告的 ETF 持仓线索（不代表基金完整持仓，也不是买入证明）：\n"
                 + json.dumps(exposure_evidence, ensure_ascii=False, separators=(",", ":")) + "\n")
                if exposure_evidence else "")

    all_rows: list[dict] = []
    rows_by_chunk: dict[int, list[dict]] = {}
    pending: dict[int, tuple[Path, str, str, list[str]]] = {}
    for index, (chunk_ids, body) in enumerate(parts, 1):
        query = (base + f"本次是整日入选新闻的第 {index}/{len(parts)} 块，共 {len(chunk_ids)} 条；"
                 "完整读取本块新闻并给出最终利好、利空或放弃结论。只用 record_numbers 引用记录序号，"
                 "合并原 ID 仅供来源追溯。\n\n" + clue_context(chunk_ids)
                 + _indexed_news(body, chunk_ids))
        fingerprint = _request_fingerprint(worker_signature, query)
        path = root / "results" / "refined_chunks" / day.isoformat() / f"chunk-{index:04d}.json"
        if _result_is_current(path, fingerprint):
            rows_by_chunk[index] = _refined_worker_rows(path, fingerprint, definition, query,
                                                         chunk_ids, records, universe, base, exposure_by_id)
            print(json.dumps({"stage": "refined_chunk_cached", "chunk": index,
                              "chunks": len(parts)}, ensure_ascii=False), flush=True)
        else:
            pending[index] = (path, fingerprint, query, chunk_ids)

    if pending:
        from agentloom.execution.concurrency import ParallelAgentExecutor
        from agentloom.execution.concurrency.models import TaskResult

        def analyze_chunk(task_id: str, query: str) -> list[dict]:
            path, fingerprint, _, allowed = pending[int(task_id)]
            return _refined_worker_rows(path, fingerprint, definition, query,
                                        allowed, records, universe, base, exposure_by_id)

        def report_chunk(_completed: int, _total: int, result: TaskResult) -> None:
            index = int(result.task_id)
            status = result.status
            print(json.dumps({"stage": "refined_chunk_finished", "chunk": index,
                              "chunks": len(parts), "status": status,
                              "selected_records": len(pending[index][3]),
                              "event_etf_rows": len(result.result) if status == "completed" else None,
                              "error": result.error if status != "completed" else None},
                             ensure_ascii=False), flush=True)

        executor = ParallelAgentExecutor(max_workers=baseline_settings.refined_workers,
                                         max_pending=baseline_settings.refined_workers,
                                         model_type="codex_luna_news_replay",
                                         circuit_breaker_threshold=None)
        outcomes = executor.execute_batch(
            [{"task_id": str(index), "query": pending[index][2]} for index in pending],
            analyze_chunk, on_progress=report_chunk,
        )
        failed = [result for result in outcomes if result.status != "completed"]
        for result in outcomes:
            if result.status == "completed":
                rows_by_chunk[int(result.task_id)] = result.result
        if failed:
            raise RuntimeError("精筛块失败：" + "；".join(
                f"{result.task_id}: {result.error}" for result in failed))

    for index, (chunk_ids, _) in enumerate(parts, 1):
        rows = rows_by_chunk[index]
        for row in rows:
            identified = _identified_refined_event(index, row)
            all_rows.append(identified)

    final_rows = merge_etf_decisions(all_rows)
    final_rows = _checked_events(final_rows, set(selected), records, universe)
    review_dir = root / "results" / "refined_chunks" / day.isoformat()
    reviews: list[dict] = []
    for index in range(1, len(parts) + 1):
        path = review_dir / f"chunk-{index:04d}.json"
        for review in json.loads(path.read_text(encoding="utf-8"))["reviews"]:
            review_event_id = (_identified_refined_event(index, {"event_id": review["event_id"]})["event_id"]
                          if review["event_id"] is not None else None)
            reviews.append({**review, "event_id": review_event_id, "stage": "chunk", "chunk": index})
    _atomic_json(review_path, {"day": day.isoformat(), "selected_records": len(selected),
                               "reviews": reviews, "mapping_signals": all_rows,
                               "final_signals": final_rows})
    return final_rows


def run_day(args: argparse.Namespace, universe: dict[str, str], baseline_settings: BaselineSettings) -> dict:
    from .evaluation.baseline import analysis_fingerprint, prediction_contract
    from .processing.results import (day_records, mark_refined_complete, read_day_results,
                                     validate_initial_results)
    from .report import write_day_report

    day = args.day
    root = args.input_root
    settings = _settings(root, args.prices, args.candidates, day)
    manifest = _prepared_day(root, day, args.expect_records, args.expect_batches)
    model_signature = _model_signature()
    initial_prompt = (ROOT / "agent" / "初筛.md").read_text(encoding="utf-8")
    records = day_records(settings, day)
    initial_context = f"mode=historical_replay；历史日期={day.isoformat()}。"
    pending: dict[str, tuple[dict, Path, str, str]] = {}
    for batch in manifest["batches"]:
        path = root / "days" / day.isoformat() / batch["file"]
        result_path = root / "results" / "initial" / day.isoformat() / batch["file"].replace(".txt", ".json")
        count = len(batch["ids"])
        query = (f"mode=historical_replay；历史日期={day.isoformat()}。本批次共有 {count} 条新闻，"
                 f"序号从 1 至 {count}；逐条完整阅读并返回全部 classifications。\n\n"
                 + _indexed_news(path.read_text(encoding="utf-8"), batch["ids"]))
        fingerprint = _request_fingerprint(_stage_signature("initial"), initial_prompt,
                                           (ROOT / "workflows" / "initial.yaml").read_text(encoding="utf-8"), query)
        if _result_is_current(result_path, fingerprint):
            print(json.dumps({"stage": "worker_cached", "workflow": "initial.yaml",
                              "cache": str(result_path)}, ensure_ascii=False), flush=True)
            continue
        if args.max_initial_batches is not None and len(pending) >= args.max_initial_batches:
            break
        pending[batch["file"]] = (batch, result_path, fingerprint, query)

    def analyze_initial(task_id: str, query: str) -> int:
        batch, result_path, fingerprint, _ = pending[task_id]
        answer = _classify_initial(query, batch, records, result_path, fingerprint, initial_context)
        return len(answer["selected_ids"])

    if pending:
        from agentloom.execution.concurrency import ParallelAgentExecutor
        from agentloom.execution.concurrency.models import TaskResult

        def report_initial(_completed: int, _total: int, result: TaskResult) -> None:
            print(json.dumps({"stage": "initial", "batch": result.task_id,
                              "selected": result.result if result.status == "completed" else None,
                              "status": result.status,
                              "error": result.error if result.status != "completed" else None},
                             ensure_ascii=False), flush=True)

        executor = ParallelAgentExecutor(max_workers=baseline_settings.initial_workers,
                                         max_pending=baseline_settings.initial_workers,
                                         model_type="codex_luna_news_initial",
                                         circuit_breaker_threshold=None)
        outcomes = executor.execute_batch(
            [{"task_id": name, "query": values[3]} for name, values in pending.items()],
            analyze_initial, on_progress=report_initial,
        )
        failed = [result for result in outcomes if result.status != "completed"]
        if failed:
            raise RuntimeError("初筛批次失败：" + "；".join(
                f"{result.task_id}: {result.error}" for result in failed))

    progress = validate_initial_results(settings, day)
    if args.max_initial_batches is not None or progress["complete_batches"] != progress["total_batches"]:
        return {"status": "pending_initial", **progress}

    selected: list[str] = []
    for batch in manifest["batches"]:
        path = root / "results" / "initial" / day.isoformat() / batch["file"].replace(".txt", ".json")
        selected.extend(json.loads(path.read_text(encoding="utf-8"))["selected_ids"])
    final_events = _analyze_selected(day, root, selected, records, universe, args,
                                     model_signature, baseline_settings)
    review_path = root / "results" / "reviews" / f"{day.isoformat()}.json"
    audit = json.loads(review_path.read_text(encoding="utf-8"))
    audit["final_signals"] = final_events
    _atomic_json(review_path, audit)
    result_path = root / "results" / f"{day.isoformat()}.jsonl"
    refined_prompt = (ROOT / "agent" / "精筛.md").read_text(encoding="utf-8")
    fingerprint = _request_fingerprint(model_signature, refined_prompt,
                                       json.dumps(final_events, ensure_ascii=False, sort_keys=True),
                                       json.dumps({key: str(value) for key, value in _limits(args).items()}, sort_keys=True),
                                       str(args.hold_days), args.sell_at)
    generated = not _result_is_current(result_path, fingerprint)
    if generated:
        value = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in final_events)
        _record_result(result_path, fingerprint, value)
    try:
        rows = read_day_results(settings, day, universe, records=records,
                                require_complete_initial=True,
                                mark_complete=False)
        _atomic_json(result_path.with_suffix(".meta.json"), {
            "fingerprint": fingerprint, "sha256": _sha256(result_path),
            "prediction": prediction_contract(args.hold_days, args.sell_at, _limits(args)),
            "analysis_fingerprint_version": 9,
            "analysis_fingerprint": analysis_fingerprint(settings, day),
            "reviews_sha256": _sha256(review_path),
        })
    except Exception:
        if generated:
            result_path.unlink(missing_ok=True)
            result_path.with_suffix(".meta.json").unlink(missing_ok=True)
        raise
    mark_refined_complete(settings, day, len(rows))
    write_day_report(settings, day, universe, rows=rows, records=records)
    return {"status": "complete", "initial": progress, "events": len(rows)}


def _limits(args: argparse.Namespace) -> dict[str, Decimal]:
    return {name: getattr(args, name) for name in LEVEL_THRESHOLD_KEYS}


def main() -> None:
    baseline_settings = load_baseline_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    copy = commands.add_parser("copy-prices", help="Copy and verify ETF daily Parquet data")
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--target", type=Path, default=baseline_settings.prices)
    copy.add_argument("--candidates", type=Path, default=baseline_settings.candidates)
    evaluate = commands.add_parser("evaluate", help="Score a completed historical news day")
    run = commands.add_parser("run", help="Run both Pi stages, then score the historical day")
    for command in (evaluate, run):
        command.add_argument("--day", type=date.fromisoformat, required=True)
        command.add_argument("--input-root", type=Path, default=baseline_settings.evaluation_root)
        command.add_argument("--prices", type=Path, default=baseline_settings.prices)
        command.add_argument("--candidates", type=Path, default=baseline_settings.candidates)
        command.add_argument("--expect-records", type=int, default=None)
        command.add_argument("--expect-batches", type=int, default=None)
        command.add_argument("--hold-days", type=int, default=baseline_settings.hold_days)
        command.add_argument("--sell-at", choices=("open", "close"), default=baseline_settings.sell_at)
        for name, default in baseline_settings.thresholds.items():
            command.add_argument(f"--{name.replace('_', '-')}", type=Decimal, default=default)
    run.add_argument("--max-initial-batches", type=int, default=None,
                     help="For a bounded smoke run, process at most this many new initial batches and stop")
    run.add_argument("--predict-only", action="store_true",
                     help="Finish refinement decisions; leave returns unread until the formal list is fixed")
    args = parser.parse_args()
    if args.command in {"evaluate", "run"}:
        validate_baseline_prediction(args.hold_days, args.sell_at, _limits(args))
    if args.command == "copy-prices":
        manifest = copy_prices(args.source, args.target, args.candidates)
        print(json.dumps({"copied_etfs": len(manifest["files"]), "target": str(args.target)}, ensure_ascii=False))
    elif args.command in {"evaluate", "run"}:
        from .evaluation.baseline import evaluate_day, write_evaluation

        universe = read_candidates(args.candidates)
        price_copy = verify_local_prices(args.prices, universe)
        _prepared_day(args.input_root, args.day, args.expect_records, args.expect_batches)
        if args.command == "run":
            stage = run_day(args, universe, baseline_settings)
            if stage["status"] != "complete":
                print(json.dumps(stage, ensure_ascii=False))
                return
            if args.predict_only:
                print(json.dumps(stage, ensure_ascii=False))
                return
        settings = _settings(args.input_root, args.prices, args.candidates, args.day)
        limits = _limits(args)
        summary, details = evaluate_day(settings, args.day, universe, hold_days=args.hold_days,
                                        sell_at=args.sell_at, limits=limits)
        summary["price_copy_sha256"] = hashlib.sha256(json.dumps(price_copy["files"], sort_keys=True).encode()).hexdigest()
        path = write_evaluation(args.input_root, args.day, summary, details, prices=args.prices)
        print(json.dumps({"report": str(path), "direction_hit_rate": summary["direction_hit_rate"],
                          "rows": len(details)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
