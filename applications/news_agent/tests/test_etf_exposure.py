"""Historical ETF exposure must use the report available before the news day."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from news_agent.evaluation.prepare_etf_exposure import (
    _fetch_rows, collect, collect_namechanges, exposure_as_of, holding_mentions_as_of,
    stock_name_as_of, stock_names_as_of,
)


class Frame:
    columns = ("ts_code", "ann_date", "end_date", "symbol", "mkv", "amount",
               "stk_mkv_ratio", "stk_float_ratio")

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self.rows


class Pro:
    def fund_portfolio(self, *, ts_code: str, start_date: str, end_date: str) -> Frame:
        assert ts_code == "159516.SZ"
        assert start_date == "20260101" and end_date == "20260901"
        return Frame([
            {"ts_code": ts_code, "ann_date": "20260721", "end_date": "20260630",
             "symbol": "688012.SH", "mkv": 100.0, "amount": 10.0,
             "stk_mkv_ratio": 15.28, "stk_float_ratio": 1.52},
            {"ts_code": ts_code, "ann_date": "20260831", "end_date": "20260630",
             "symbol": "688012.SH", "mkv": 100.0, "amount": 10.0,
             "stk_mkv_ratio": 15.10, "stk_float_ratio": 1.52},
            {"ts_code": ts_code, "ann_date": "20260831", "end_date": "20260630",
             "symbol": "002371.SZ", "mkv": 90.0, "amount": 10.0,
             "stk_mkv_ratio": 13.34, "stk_float_ratio": 1.20},
        ])


def test_exposure_uses_publication_date_not_report_period(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.txt"
    candidates.write_text("159516.SZ\t半导体设备\n", encoding="utf-8")
    output = tmp_path / "exposure"
    manifest = collect(Pro(), candidates, output,
                       start=date(2026, 1, 1), end=date(2026, 9, 1))
    assert manifest["funds"] == 1 and manifest["rows"] == 3
    august = exposure_as_of(output, "159516.SZ", date(2026, 8, 3))
    assert august["ann_date"] == "20260721"
    assert august["reported_positions"] == 1
    assert august["positions"][0]["stk_mkv_ratio"] == 15.28
    assert exposure_as_of(output, "159516.SZ", date(2026, 7, 21))["status"] == "no_prior_report"
    september = exposure_as_of(output, "159516.SZ", date(2026, 9, 1))
    assert september["ann_date"] == "20260831"
    assert september["reported_positions"] == 2


def test_capped_api_response_is_split_without_losing_rows() -> None:
    class CappedPro:
        calls = 0

        def fund_portfolio(self, *, ts_code: str, start_date: str, end_date: str) -> Frame:
            self.calls += 1
            rows = [
                {"ts_code": ts_code,
                 "ann_date": "20260101" if index < 1000 else "20260103",
                 "end_date": "20251231", "symbol": f"{index:06d}.SH",
                 "mkv": 100.0, "amount": 10.0,
                 "stk_mkv_ratio": 0.01, "stk_float_ratio": 0.01}
                for index in range(2001)
            ]
            return Frame([row for row in rows
                          if start_date <= str(row["ann_date"]) <= end_date][:2000])

    pro = CappedPro()
    rows = _fetch_rows(pro, "510300.SH", date(2026, 1, 1), date(2026, 1, 3))
    assert len(rows) == 2001
    assert len({row["symbol"] for row in rows}) == 2001
    assert pro.calls == 3


def test_historical_stock_name_excludes_future_rename(tmp_path: Path) -> None:
    class NameFrame:
        columns = ("ts_code", "name", "start_date", "end_date", "ann_date")

        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows

        def __len__(self) -> int:
            return len(self.rows)

        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return self.rows

    class NamePro:
        def namechange(self, *, start_date: str, end_date: str) -> NameFrame:
            rows = [
                {"ts_code": "688012.SH", "name": "中微公司", "start_date": "20190722",
                 "end_date": "20260831", "ann_date": "20190722"},
                {"ts_code": "688012.SH", "name": "未来改名", "start_date": "20260901",
                 "end_date": None, "ann_date": "20260830"},
                {"ts_code": "600388.SH", "name": "旧名", "start_date": "20200101",
                 "end_date": "20260816", "ann_date": "20200101"},
                {"ts_code": "600388.SH", "name": "新名", "start_date": "20260422",
                 "end_date": None, "ann_date": "20260422"},
            ]
            return NameFrame([row for row in rows
                              if start_date <= row["ann_date"] <= end_date])

    output = tmp_path / "names"
    manifest = collect_namechanges(NamePro(), output,
                                   start=date(2019, 1, 1), end=date(2026, 9, 30))
    assert manifest["rows"] == 4
    assert stock_name_as_of(output, "688012.SH", date(2026, 8, 3))["name"] == "中微公司"
    assert stock_name_as_of(output, "688012.SH", date(2026, 8, 31))["name"] == "中微公司"
    assert stock_name_as_of(output, "688012.SH", date(2026, 9, 1))["name"] == "未来改名"
    assert stock_name_as_of(output, "688012.SH", date(2018, 1, 1))["status"] == "unknown"
    names = stock_names_as_of(output, {"688012.SH", "600388.SH"}, date(2026, 8, 3))
    assert names["688012.SH"]["name"] == "中微公司"
    assert names["600388.SH"]["status"] == "ambiguous"
    candidates = tmp_path / "candidates.txt"
    candidates.write_text("159516.SZ\t半导体设备\n", encoding="utf-8")
    collect(Pro(), candidates, output, start=date(2026, 1, 1), end=date(2026, 9, 1))
    mentions = holding_mentions_as_of(output, date(2026, 8, 3), {
        "N1": "中微公司披露上半年业绩", "N2": "未来改名披露上半年业绩"})
    assert [row["fund"] for row in mentions["N1"]] == ["159516.SZ"]
    assert mentions["N1"][0]["stk_mkv_ratio"] == 15.28
    assert mentions["N1"][0]["match_basis"] == "headline_stock_name"
    assert "N2" not in mentions
    issuer = holding_mentions_as_of(output, date(2026, 8, 3),
                                    {"A1": "2026年半年度报告"},
                                    {"A1": "688012.SH"})
    assert [row["fund"] for row in issuer["A1"]] == ["159516.SZ"]
    assert issuer["A1"][0]["match_basis"] == "announcement_issuer_code"
    assert not holding_mentions_as_of(output, date(2026, 7, 21),
                                      {"N1": "中微公司披露上半年业绩"},
                                      {"N1": "688012.SH"})
