"""Keep exact news provenance while models reference local record numbers."""

import json
from pathlib import Path

import pytest
import yaml

from news_agent import baseline


IDS = ["N20250731-74d31722fb27944618034b9a",
       "N20250731-cf6d643c9650de0b942b9411"]


def test_provider_schema_keeps_integer_arrays_without_unsupported_uniqueness():
    schema = yaml.safe_load((baseline.ROOT / "workflows/refined.yaml").read_text())["output_schema"]
    for group in ("reviews",):
        numbers = schema["properties"][group]["items"]["properties"]["record_numbers"]
        assert numbers["type"] == "array" and numbers["minItems"] == 1
        assert numbers["items"] == {"type": "integer", "minimum": 1}
        assert "uniqueItems" not in numbers


def _answer(numbers):
    return { "reviews": [{
        "event_id": None, "etf_code": None, "record_numbers": numbers,
        "summary": "同一旧事实的两篇报道",
        "decision": "放弃",
        "reason": "已核对首披，未发现新增进展", "sources": []}]}


def _run(tmp_path, ids=IDS):
    return baseline._refined_worker_rows(
        tmp_path / "chunk.json", "current-method", tmp_path / "refined.yaml",
        '证据截止=2025-08-17T23:59:59+08:00；完整正文', ids,
        {rid: {"record_id": rid, "record_ids": [rid], "members": [], "metadata": {},
               "category": "新闻快讯", "source": "fixture", "published_at": "2025-08-17",
               "time_precision": "date", "title": rid, "text": "完整正文", "url": ""} for rid in ids},
        {"510300.SH": "宽基"}, '证据截止=2025-08-17T23:59:59+08:00', {})


def test_numbered_refinement_restores_every_exact_original_id_and_reuses_current_cache(
        tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(baseline, "_call_worker",
                        lambda *args, **_kwargs: calls.append(args) or _answer([1, 2]))
    assert _run(tmp_path) == []
    assert _run(tmp_path) == []
    assert len(calls) == 1
    saved = json.loads((tmp_path / "chunk.json").read_text())
    assert saved["reviews"][0]["record_ids"] == IDS
    assert "record_numbers" not in saved["reviews"][0]


@pytest.mark.parametrize("numbers", [[0, 2], [1, 3], [True, 2], [1.0, 2],
                                      ["1", 2], [1, 1]])
def test_invalid_numbered_output_never_becomes_success(
        tmp_path: Path, monkeypatch, numbers):
    calls = []
    monkeypatch.setattr(baseline, "_call_worker",
                        lambda *args, **_kwargs: calls.append(args) or _answer(numbers))
    with pytest.raises(ValueError):
        _run(tmp_path)
    assert len(calls) == 1
    assert not (tmp_path / "chunk.json").exists()
    assert not (tmp_path / "chunk.meta.json").exists()


def test_supplement_uses_its_own_local_numbering(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(baseline, "_call_worker",
                        lambda *args, **_kwargs: calls.append(args) or _answer([1]))
    assert _run(tmp_path) == []
    assert len(calls) == 2
    saved = json.loads((tmp_path / "chunk.json").read_text())
    assert [row["record_ids"] for row in saved["reviews"]] == [[IDS[0]], [IDS[1]]]


@pytest.mark.parametrize("record_id", [IDS[0], IDS[0][:-1], IDS[0][:-1] + "?"])
def test_old_model_id_wire_is_rejected_even_when_the_id_is_complete(
        tmp_path: Path, monkeypatch, record_id):
    answer = _answer([1, 2])
    answer["reviews"][0].pop("record_numbers")
    answer["reviews"][0]["record_ids"] = [record_id, IDS[1]]
    monkeypatch.setattr(baseline, "_call_worker", lambda *_, **_kwargs: answer)
    with pytest.raises(ValueError, match="record_numbers"):
        _run(tmp_path)
    assert not (tmp_path / "chunk.json").exists()
