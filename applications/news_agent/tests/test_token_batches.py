"""Check news coverage and limits in tokens, including old-cache invalidation."""

from dataclasses import replace
from datetime import date
import json

import pyarrow as pa
import pyarrow.parquet as pq

from news_agent.processing.prepare import chunk_selected_records, count_tokens, prepare_day
from news_agent.settings import load_settings


def _record(record_id, text):
    return {"record_id": record_id, "record_ids": [record_id], "members": [],
            "metadata": {}, "category": "新闻快讯", "source": "测试", "title": record_id,
            "published_at": "2025-08-14", "time_precision": "date", "url": "", "text": text}


def test_token_batches_do_not_treat_characters_as_tokens():
    ids = ["N1", "N2"]
    ascii_records = {item: _record(item, "A" * 4000) for item in ids}
    chinese_records = {item: _record(item, "你好世界" * 500) for item in ids}
    ascii_chunks = chunk_selected_records(ids, ascii_records, 1500, "o200k_base")
    chinese_chunks = chunk_selected_records(ids, chinese_records, 1500, "o200k_base")
    assert len(ascii_chunks) == 1 and len(ascii_chunks[0][1]) > 1500
    assert len(chinese_chunks) == 2
    for records, chunks in ((ascii_records, ascii_chunks), (chinese_records, chinese_chunks)):
        assert [item for group, _ in chunks for item in group] == ids
        assert all(count_tokens(body, "o200k_base") <= 1500 for _, body in chunks)


def test_preparation_records_tokenizer_and_rebuilds_when_token_budget_changes(tmp_path):
    raw = tmp_path / "news/raw/新闻快讯/date=2025-08-14/data.parquet"
    raw.parent.mkdir(parents=True)
    pq.write_table(pa.table({"datetime": ["2025-08-14 10:00:00"] * 3,
                             "title": ["N1", "N2", "N3"],
                             "content": ["你好世界" * 500] * 3}), raw)
    settings = replace(load_settings(), news_root=tmp_path / "news",
                       output_root=tmp_path / "inputs", initial_max_tokens=1500)
    first = prepare_day(settings, date(2025, 8, 14))
    assert first["raw_count"] == first["prepared_count"] == 3
    assert len(first["batches"]) == 3
    assert first["tokenizer"]["encoding"] == "o200k_base"
    assert first["tokenizer"]["provider_measured"] is False
    folder = settings.output_root / "days/2025-08-14"
    for batch in first["batches"]:
        assert batch["tokens"] == count_tokens((folder / batch["file"]).read_text(), settings.token_encoding)
        assert not batch["exceeds_news_token_limit"]
    enlarged = prepare_day(replace(settings, initial_max_tokens=2500), date(2025, 8, 14))
    assert len(enlarged["batches"]) == 2
    assert first["records_sha256"] == enlarged["records_sha256"]
    assert [item for batch in first["batches"] for item in batch["ids"]] == [
        item for batch in enlarged["batches"] for item in batch["ids"]]
    segmented = prepare_day(replace(settings, initial_max_tokens=256), date(2025, 8, 14))
    assert all(not batch["exceeds_news_token_limit"] for batch in segmented["batches"])
    originals = [json.loads(line) for line in (folder/"records-original.jsonl").read_text().splitlines()]
    records = [json.loads(line) for line in (folder/"records.jsonl").read_text().splitlines()]
    for original in originals:
        pieces = [record for record in records if record.get("segment", {}).get("parent_record_id") == original["record_id"]]
        assert "".join(piece["text"] for piece in pieces) == original["text"]
        assert pieces[0]["segment"]["start_char"] == 0
        assert pieces[-1]["segment"]["end_char"] == len(original["text"])
    assert segmented["prepared_count"] > segmented["original_prepared_count"] == 3
