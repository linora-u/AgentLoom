from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from ..settings import Settings
from .temporal_integrity import inaccessible_us_close_body


SHANGHAI = timezone(timedelta(hours=8))
FORMAT_VERSION = 6
SOURCE_FIELDS = {
    "国家政策库": ("pubtime", "content_html"),
    "央行货币政策报告": ("pub_date", "content_html"),
    "长篇新闻": ("pub_time", "content"),
    "新闻联播文字稿": ("date", "content"),
    "新闻快讯": ("datetime", "content"),
}
TIME_FIELDS = ("pubtime", "ann_date", "rec_time", "pub_date", "pub_time", "date", "trade_date", "datetime")
DATE_ONLY_FIELDS = {"ann_date", "pub_date", "date", "trade_date"}
METADATA_FIELDS = ("ts_code", "name", "inst_csname", "ind_name", "report_type", "author",
                   "puborg", "ptype", "pcode", "channels", "source_code")
BOILERPLATE = re.compile(r"^(责任编辑|免责声明|广告|扫码关注|文章来源|点击查看|相关阅读|分享到|返回顶部)[:：]?.*$")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    parser = _VisibleText()
    parser.feed(str(value))
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line and not BOILERPLATE.match(line))


def _value(value: Any) -> str:
    if value is None or str(value).lower() in {"nan", "nat", "none"}:
        return ""
    return str(value).strip()


def _publish_time(value: Any) -> tuple[str | None, str]:
    raw = _value(value)
    if not raw:
        return None, "unknown"
    if re.fullmatch(r"\d{8}", raw):
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:])).isoformat(), "date"
    if re.fullmatch(r"\d{4}-\d\d-\d\d", raw):
        return date.fromisoformat(raw).isoformat(), "date"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI).isoformat(timespec="seconds"), "datetime"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _files_for_day(settings: Settings, day: date) -> list[Path]:
    raw = settings.news_root / "raw"
    paths: list[Path] = []
    for category in SOURCE_FIELDS:
        paths.extend((raw / category / f"date={day.isoformat()}").rglob("data.parquet"))
    return sorted(paths)


def _source_signature(settings: Settings, paths: list[Path]) -> list[dict[str, Any]]:
    dependencies = list(paths)
    for path in paths:
        if path.relative_to(settings.news_root / "raw").parts[0] != "央行货币政策报告":
            continue
        folder = settings.news_root / "documents" / "央行货币政策报告" / path.parent.name
        for manifest in folder.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{64}\.json", manifest.name):
                document = json.loads(manifest.read_text(encoding="utf-8"))
                dependencies.extend((manifest, folder / document["text_path"],
                                     folder / document["pdf_path"]))
    return [
        {
            "path": path.relative_to(settings.news_root).as_posix(),
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in sorted(set(dependencies))
    ]


def _identity_digest(path: Path, row: dict[str, Any], root: Path) -> str:
    content = {key: value for key, value in row.items() if key not in {"_record_hash", "_record_date"}}
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256((path.relative_to(root).as_posix() + "\n" + raw).encode()).hexdigest()


def _normalize(day: date, category: str, path: Path, index: int, row: dict[str, Any], root: Path,
               digest: str, occurrence: int) -> dict[str, Any]:
    time_field, body_field = SOURCE_FIELDS[category]
    raw_path = path.relative_to(root).as_posix()
    record_id = "N" + day.strftime("%Y%m%d") + "-" + digest[:24] + (f"-{occurrence}" if occurrence > 1 else "")
    published_at, precision = _publish_time(row.get(time_field))
    if time_field in DATE_ONLY_FIELDS and published_at:
        published_at, precision = published_at[:10], "date"
    text = clean_text(row.get(body_field)) if body_field else ""
    document = None
    if category == "央行货币政策报告":
        folder = root / "documents" / category / f"date={day.isoformat()}"
        manifest = folder / f"{_value(row.get('_record_hash'))}.json"
        if not manifest.is_file():
            raise ValueError(f"{day}: 央行报告只有摘要，缺少完整 PDF 正文: {manifest}")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        full_text = (folder / document["text_path"]).read_bytes()
        if (document["raw_hash"] != _value(row.get("_record_hash"))
                or document["declared_pdf_url"] != _value(row.get("pdf_url"))
                or hashlib.sha256(full_text).hexdigest() != document["text_sha256"]):
            raise ValueError(f"{day}: 央行报告全文与来源记录不一致: {manifest}")
        text = full_text.decode("utf-8").strip()
        if not text:
            raise ValueError(f"{day}: 央行报告 PDF 正文为空: {manifest}")
    title = clean_text(row.get("title"))
    url = _value(row.get("url")) or _value(row.get("pdf_url"))
    metadata = {field: _value(row.get(field)) for field in METADATA_FIELDS if _value(row.get(field))}
    if document is not None:
        metadata.update({"pdf_source_url": document["download_url"],
                         "pdf_version_basis": document["version_basis"],
                         "pdf_pages": str(document["pages"]),
                         "pdf_sha256": document["pdf_sha256"]})
    source_from_path = next((part.split("=", 1)[1] for part in path.parts if part.startswith("source=")), "")
    source = _value(row.get("source")) or _value(row.get("src")) or source_from_path or category
    raw_times = {field: _value(row.get(field)) for field in TIME_FIELDS if _value(row.get(field))}
    member = {"record_id": record_id, "source": source, "published_at": published_at,
              "time_precision": precision, "raw_times": raw_times, "raw_path": raw_path,
              "raw_row": index, "url": url}
    return {
        "record_id": record_id,
        "record_ids": [record_id],
        "members": [member],
        "raw_hash": _value(row.get("_record_hash")) or None,
        "raw_path": raw_path,
        "raw_row": index,
        "category": category,
        "source": source,
        "title": title,
        "text": text,
        "metadata": metadata,
        "url": url,
        "document_url": document["download_url"] if document is not None else _value(row.get("pdf_url")),
        "body_status": "read" if text else "unread",
        "raw_times": raw_times,
        "published_at": published_at,
        "time_precision": precision,
    }


def _dedup_key(record: dict[str, Any]) -> tuple[str, str] | None:
    context = record["title"] + "\n" + record["text"] + "\n" + record["metadata"].get("ts_code", "") + "\n" + record["metadata"].get("name", "")
    digest = hashlib.sha256(context.encode()).hexdigest()
    if record["url"] and record["category"] == "央行货币政策报告":
        return "document_url", record["url"].split("#", 1)[0] + "|" + digest
    if record["text"]:
        return "exact_content", digest
    return None


def _record_input(record: dict[str, Any]) -> str:
    duplicates = ""
    if len(record["record_ids"]) > 1:
        duplicates = "合并原ID: " + ", ".join(record["record_ids"]) + "\n"
        duplicates += "来源时间: " + "；".join(f"{item['record_id']} {item['source']} {item['published_at'] or '未知'}" for item in record["members"]) + "\n"
    metadata = ""
    if record["metadata"]:
        metadata = "补充信息: " + "；".join(f"{key}={value}" for key, value in record["metadata"].items()) + "\n"
    return (
        f"记录ID: {record['record_id']}\n"
        f"{duplicates}"
        f"类别/来源: {record['category']} / {record['source']}\n"
        f"{metadata}"
        f"公开时间: {record['published_at'] or '未知'} ({record['time_precision']})\n"
        f"标题: {record['title'] or '无标题'}\n"
        f"正文: {record['text'] or '[正文未读取，仅可依据标题判断；链接内容未读取]'}\n"
        f"链接: {record['url'] or '无'}\n\n"
    )


def count_tokens(text: str, token_encoding: str) -> int:
    """Count with the configured local tokenizer, independently of character count."""
    import tiktoken

    return len(tiktoken.get_encoding(token_encoding).encode(text, disallowed_special=()))


def chunk_selected_records(selected: list[str], records: dict[str, dict[str, Any]],
                           max_tokens: int, token_encoding: str) -> list[tuple[list[str], str]]:
    """Group complete records; oversized articles are segmented in preparation."""
    if max_tokens < 1:
        raise ValueError("新闻批次 token 数必须大于零")
    chunks: list[tuple[list[str], str]] = []
    current_ids: list[str] = []
    current_text: list[str] = []
    current_size = 0
    for record_id in selected:
        source_text = _record_input(records[record_id])
        source_tokens = count_tokens(source_text, token_encoding)
        if source_tokens > max_tokens:
            raise ValueError(f"{record_id}: 新闻尚未完整分段，不能突破 token 上限")
        if current_ids and current_size + source_tokens > max_tokens:
            chunks.append((current_ids, "".join(current_text)))
            current_ids, current_text, current_size = [], [], 0
        current_ids.append(record_id)
        current_text.append(source_text)
        current_size += source_tokens
    if current_ids:
        chunks.append((current_ids, "".join(current_text)))
    return chunks


def _segment_record(record: dict[str, Any], maximum: int, encoding: str) -> list[dict[str, Any]]:
    """Split on Unicode character boundaries, with exact offsets and parent IDs."""
    if count_tokens(_record_input(record), encoding) <= maximum:
        return [record]
    text = record["text"]
    parent = record["record_id"]
    pieces: list[dict[str, Any]] = []
    offset = 0
    while offset < len(text):
        def segment(end):
            identifier = parent[:10] + hashlib.sha256(f"{parent}:{offset}:{end}".encode()).hexdigest()[:24]
            members = [{**record["members"][0], "record_id": identifier}]
            return {**record, "record_id": identifier, "record_ids": [identifier], "members": members,
                "text": text[offset:end], "source_record_ids": record["record_ids"],
                "segment": {"parent_record_id": parent, "index": len(pieces)+1,
                            "start_char": offset, "end_char": end, "original_chars": len(text)},
                "metadata": {**record["metadata"], "原记录": parent,
                             "完整分段序号": len(pieces)+1, "正文字符区间": f"{offset}:{end}/{len(text)}"}}
        low, high = offset+1, len(text)
        best = None
        while low <= high:
            end = (low+high)//2
            candidate = segment(end)
            if count_tokens(_record_input(candidate), encoding) <= maximum:
                best = candidate
                low = end+1
            else:
                high = end-1
        if best is None:
            raise ValueError(f"{parent}: 元数据超过 token 预算，不能静默截断")
        pieces.append(best)
        offset = best["segment"]["end_char"]
    if not pieces or "".join(piece["text"] for piece in pieces) != text:
        raise ValueError(f"{parent}: 完整分段正文校验失败")
    for piece in pieces:
        piece["segment"]["total"] = len(pieces)
    return pieces


def prepare_day(settings: Settings, day: date) -> dict[str, Any]:
    from importlib.metadata import version

    maximum = settings.initial_max_tokens
    if maximum is None:
        raise ValueError("新闻预处理需要 initial_max_tokens")
    tokenizer = {"encoding": settings.token_encoding, "library": "tiktoken",
                 "version": version("tiktoken"), "provider_measured": False}
    paths = _files_for_day(settings, day)
    summary_path = settings.news_root / "_manifest" / "download_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        cutoff = summary.get("available_end_date", summary.get("cutoff_date"))
        first = summary.get("available_start_date")
        if first and day < date.fromisoformat(first):
            raise ValueError(f"{day}: 本地新闻起始 {first}，请先导入所需日期")
        if cutoff and day > date.fromisoformat(cutoff):
            raise ValueError(f"{day}: 新闻下载截止 {cutoff}，不能分析未完成下载的日期")
    signature = _source_signature(settings, paths)
    target = settings.output_root / "days" / day.isoformat()
    manifest_path = target / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (old.get("format_version") == FORMAT_VERSION and old.get("source_files") == signature
                and old.get("initial_max_tokens") == maximum and old.get("tokenizer") == tokenizer
                and old.get("segmentation_max_tokens") == min(maximum, settings.refined_max_tokens)
                and (target / "records.jsonl").exists()
                and hashlib.sha256((target / "records.jsonl").read_bytes()).hexdigest() == old.get("records_sha256")
                and all((target / batch["file"]).exists() and hashlib.sha256((target / batch["file"]).read_bytes()).hexdigest() == batch["sha256"] for batch in old["batches"])):
            return old

    records: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    duplicates: dict[str, str] = {}
    raw_count = 0
    for path in paths:
        category = path.relative_to(settings.news_root / "raw").parts[0]
        row_index = 0
        identity_counts: dict[str, int] = {}
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=1024):
            for row in batch.to_pylist():
                digest = _identity_digest(path, row, settings.news_root)
                identity_counts[digest] = identity_counts.get(digest, 0) + 1
                record = _normalize(day, category, path, row_index, row, settings.news_root, digest, identity_counts[digest])
                if inaccessible_us_close_body(day, record):
                    record["text"] = "[历史正文版本不可用；请自主联网核对截止前的原始发布及版本。]"
                    record["body_status"] = "unavailable_future_revision"
                row_index += 1
                raw_count += 1
                key = _dedup_key(record)
                if key is not None and key in seen:
                    canonical = records[seen[key]]
                    canonical["record_ids"].append(record["record_id"])
                    canonical["members"].append(record["members"][0])
                    duplicates[record["record_id"]] = canonical["record_id"]
                else:
                    if key is not None:
                        seen[key] = len(records)
                    records.append(record)

    target.mkdir(parents=True, exist_ok=True)
    originals_text = "".join(_json(record) + "\n" for record in records)
    _atomic_text(target / "records-original.jsonl", originals_text)
    original_count = len(records)
    segmentation_limit = min(maximum, settings.refined_max_tokens)
    records = [segment for record in records
               for segment in _segment_record(record, segmentation_limit, settings.token_encoding)]
    records_text = "".join(_json(record) + "\n" for record in records)
    records_digest = hashlib.sha256(records_text.encode()).hexdigest()
    _atomic_text(target / "records.jsonl", records_text)
    batches: list[dict[str, Any]] = []
    ordered_ids = [record["record_id"] for record in records]
    by_id = {record["record_id"]: record for record in records}
    for ids, content in chunk_selected_records(ordered_ids, by_id, maximum, settings.token_encoding):
        name = f"batch-{len(batches) + 1:04d}.txt"
        tokens = count_tokens(content, settings.token_encoding)
        if tokens > maximum:
            raise ValueError(f"{day}: 新闻拼接后的 token 数超过批次上限")
        digest = hashlib.sha256(content.encode()).hexdigest()
        file = target / name
        if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            _atomic_text(file, content)
        batches.append({"file": name, "ids": ids, "count": len(ids), "chars": len(content),
                        "tokens": tokens, "exceeds_news_token_limit": tokens > maximum,
                        "sha256": digest, "state": "prepared"})
    manifest = {
        "format_version": FORMAT_VERSION,
        "day": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "source_files": signature,
        "initial_max_tokens": maximum,
        "segmentation_max_tokens": segmentation_limit,
        "original_prepared_count": original_count,
        "original_records_sha256": hashlib.sha256(originals_text.encode()).hexdigest(),
        "tokenizer": tokenizer,
        "raw_count": raw_count,
        "prepared_count": len(records),
        "records_sha256": records_digest,
        "unavailable_future_revisions": sum(record["body_status"] == "unavailable_future_revision" for record in records),
        "duplicates": duplicates,
        "batches": batches,
        "preparation_state": "complete",
    }
    _atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    coverage = {"complete_batches": 0, "total_batches": len(batches), "selected_ids": 0,
                "batch_states": {batch["file"]: "pending" for batch in batches},
                "initial_result_state": "pending" if batches else "empty",
                "refined_result_state": "pending" if batches else "empty"}
    _atomic_text(target / "coverage.json", json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")
    current = {batch["file"] for batch in batches}
    for stale in target.glob("batch-*.txt"):
        if stale.name not in current:
            stale.unlink()
    return manifest
