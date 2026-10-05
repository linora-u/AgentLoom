"""Full batch classification must retain every news record without copying hashes."""

import pytest

from news_agent import baseline


def test_full_classification_maps_every_local_index_back_to_its_original_record():
    ids = [f"N20250731-{number:024x}" for number in range(1, 401)]
    batch = {"file": "batch-0012.txt", "ids": ids}
    answer = {"classifications": [[number, 0 if number % 3 == 0 else 2]
                                  for number in range(1, 401)]}
    result = baseline._initial_classifications(answer, batch)
    assert result["selected_ids"] == ids[2::3]
    assert result["rejected"] == [{"reason_code": "行情复述",
                                   "record_ids": [key for i, key in enumerate(ids, 1) if i % 3]}]
    assert len(result["selected_ids"]) + len(result["rejected"][0]["record_ids"]) == 400


@pytest.mark.parametrize("rows", [
    [[1, 0]],                    # The observed incomplete-batch pattern.
    [[1, 0], [1, 2]],           # One record cannot be both selected and rejected.
    [[1, 0], [3, 2]],           # An index from another batch cannot enter this batch.
    [[2, 2], [1, 0]],           # Output order must follow the input.
    [[1, 0], [2, 9]],           # Every rejection needs a defined reason.
    [[1, 0], [2, True]],
])
def test_incomplete_or_conflicting_classification_is_never_accepted(rows):
    batch = {"file": "batch.txt", "ids": ["N1", "N2"]}
    with pytest.raises(ValueError):
        baseline._initial_classifications({"classifications": rows}, batch)


def test_initial_query_changes_only_record_headers_and_retains_the_full_body():
    ids = [f"N20250731-{i:024x}" for i in (1, 2)]
    body = (f"记录ID: {ids[0]}\n合并原ID: {ids[0]}\n正文: " + "完整长正文。" * 2000
            + f"\n\n记录ID: {ids[1]}\n正文: 重大政策仍有未核实首披线索。\n")
    indexed = baseline._indexed_news(body, ids)
    assert indexed == body.replace(f"记录ID: {ids[0]}\n", "记录序号: 1\n", 1).replace(
        f"记录ID: {ids[1]}\n", "记录序号: 2\n", 1)
    assert "完整长正文。" * 2000 in indexed
    assert f"合并原ID: {ids[0]}" in indexed


def test_supplement_requests_only_missing_classification_and_keeps_every_reason(tmp_path, monkeypatch):
    calls = []
    answers = iter([{"classifications": [[1, 0]]},
                    {"classifications": [[1, 6]]}])

    def worker(_definition, query):
        calls.append(query)
        return next(answers)

    monkeypatch.setattr(baseline, "_call_worker", worker)
    query = "记录序号: 1\n正文: 新政策完整正文。\n记录序号: 2\n正文: 天气。"
    records = {rid: {"record_id": rid, "record_ids": [rid], "members": [], "metadata": {},
                    "category": "新闻快讯", "source": "fixture", "published_at": "2025-07-31",
                    "time_precision": "date", "title": rid, "text": text, "url": ""}
               for rid, text in [("N1", "新政策完整正文。"), ("N2", "天气。") ]}
    result = baseline._classify_initial(query, {"file": "batch.txt", "ids": ["N1", "N2"]},
                                       records, tmp_path / "batch.json", "fingerprint", "历史日期=2025-07-31")
    assert result == {"selected_ids": ["N1"],
                      "rejected": [{"reason_code": "非财经", "record_ids": ["N2"]}]}
    assert len(calls) == 2
    assert "天气。" in calls[1] and "新政策完整正文。" not in calls[1]
