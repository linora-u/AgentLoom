"""Cache dated ETF holdings for historical exposure checks.

Tushare fund_portfolio includes both quarterly top holdings and later full
interim/annual reports for the same period. Select by announcement date, never
by the portfolio period alone. Missing stocks in a partial report are unknown,
not zero weight.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from ..baseline import read_candidates
from ..processing.prepare import _atomic_text


FUND_CODE = re.compile(r"\d{6}\.(?:SH|SZ)\Z")
SECURITY_CODE = re.compile(r"[A-Za-z0-9.]{3,24}\Z")
FIELDS = ("ts_code", "ann_date", "end_date", "symbol", "mkv", "amount",
          "stk_mkv_ratio", "stk_float_ratio")
NAME_FIELDS = ("ts_code", "name", "start_date", "end_date", "ann_date")
NAMECHANGE_ROW_CAP = 10000


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _date(value: Any) -> date:
    text = str(value)
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"invalid portfolio date: {text}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def _number(value: Any, *, required: bool = False) -> float | None:
    if value is None or str(value).lower() in {"nan", "none", "nat", ""}:
        if required:
            raise ValueError("missing holding market value or ratio")
        return None
    result = float(value)
    if not 0 <= result < float("inf"):
        raise ValueError("invalid holding market value or ratio")
    return result


def _rows(frame: Any, fund: str, start: date, end: date) -> list[dict[str, Any]]:
    if not set(FIELDS) <= set(frame.columns):
        raise ValueError(f"{fund}: fund_portfolio fields missing")
    if len(frame) >= 2000:
        raise ValueError(f"{fund}: possible API row cap; split date range")
    result: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str]] = set()
    for raw in frame.to_dict("records"):
        if raw["ts_code"] != fund or not SECURITY_CODE.fullmatch(str(raw["symbol"])):
            raise ValueError(f"{fund}: malformed fund or stock code")
        announced = _date(raw["ann_date"])
        period = _date(raw["end_date"])
        if not start <= announced <= end or period > announced:
            raise ValueError(f"{fund}: holding outside dated report window")
        key = (str(raw["ann_date"]), str(raw["end_date"]), str(raw["symbol"]))
        if key in keys:
            raise ValueError(f"{fund}: duplicate dated holding: {key}")
        keys.add(key)
        result.append({"ts_code": fund, "ann_date": str(raw["ann_date"]),
                       "end_date": str(raw["end_date"]), "symbol": str(raw["symbol"]),
                       "mkv": _number(raw["mkv"], required=True),
                       "amount": _number(raw["amount"]),
                       "stk_mkv_ratio": _number(raw["stk_mkv_ratio"], required=True),
                       "stk_float_ratio": _number(raw["stk_float_ratio"])})
    return sorted(result, key=lambda row: (row["ann_date"], row["end_date"],
                                           row["symbol"]))


def _fetch_rows(pro: Any, fund: str, start: date, end: date) -> list[dict[str, Any]]:
    frame = pro.fund_portfolio(ts_code=fund, start_date=start.strftime("%Y%m%d"),
                               end_date=end.strftime("%Y%m%d"))
    if len(frame) < 2000:
        return _rows(frame, fund, start, end)
    if start == end:
        raise ValueError(f"{fund}: API capped even for one announcement day")
    midpoint = start + timedelta(days=(end - start).days // 2)
    left = _fetch_rows(pro, fund, start, midpoint)
    right = _fetch_rows(pro, fund, midpoint + timedelta(days=1), end)
    combined = left + right
    keys = [(row["ann_date"], row["end_date"], row["symbol"]) for row in combined]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{fund}: duplicate holding across split API queries")
    return sorted(combined, key=lambda row: (row["ann_date"], row["end_date"],
                                                 row["symbol"]))


def collect(pro: Any, candidates: Path, output_root: Path,
            *, start: date, end: date) -> dict[str, Any]:
    if start > end:
        raise ValueError("start after end")
    universe = read_candidates(candidates)
    output_root.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    total_rows = 0
    for fund in sorted(universe):
        if not FUND_CODE.fullmatch(fund):
            raise ValueError(f"invalid ETF code: {fund}")
        path = output_root / f"{fund}.json"
        if path.is_file():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (cached.get("fund") != fund or cached.get("start") != start.isoformat()
                    or cached.get("end") != end.isoformat()
                    or cached.get("source") != "Tushare fund_portfolio"):
                raise ValueError(f"{fund}: incompatible exposure cache")
            rows = cached["rows"]
        else:
            rows = _fetch_rows(pro, fund, start, end)
            payload = {"fund": fund, "fund_name": universe[fund],
                       "start": start.isoformat(), "end": end.isoformat(),
                       "source": "Tushare fund_portfolio",
                       "weight_basis": "percent_of_stock_market_value_not_fund_nav",
                       "rows": rows}
            _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2,
                                          allow_nan=False) + "\n")
        files[fund] = {"path": str(path.resolve()), "sha256": _sha(path),
                       "rows": len(rows)}
        total_rows += len(rows)
        print(json.dumps({"fund": fund, "rows": len(rows)}), flush=True)
    manifest = {"source": "Tushare fund_portfolio",
                "candidate_universe_sha256": _sha(candidates),
                "start": start.isoformat(), "end": end.isoformat(),
                "funds": len(universe), "rows": total_rows,
                "files": files,
                "availability_rule": "use only ann_date strictly before news day",
                "coverage_warning": "quarterly reports may disclose only top holdings"}
    _atomic_text(output_root / "manifest.json",
                 json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def exposure_as_of(output_root: Path, fund: str, news_day: date) -> dict[str, Any]:
    if not FUND_CODE.fullmatch(fund):
        raise ValueError("invalid ETF code")
    manifest_path = output_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["files"].get(fund)
    if entry is None:
        raise ValueError(f"{fund}: fund outside exposure universe")
    path = Path(entry["path"])
    if not path.is_file() or _sha(path) != entry["sha256"]:
        raise ValueError(f"{fund}: exposure source digest mismatch")
    source = json.loads(path.read_text(encoding="utf-8"))
    prior = [row for row in source["rows"] if _date(row["ann_date"]) < news_day]
    if not prior:
        return {"fund": fund, "news_day": news_day.isoformat(),
                "status": "no_prior_report", "positions": []}
    latest_announcement = max(row["ann_date"] for row in prior)
    latest_period = max(row["end_date"] for row in prior
                        if row["ann_date"] == latest_announcement)
    positions = sorted((row for row in prior if row["ann_date"] == latest_announcement
                        and row["end_date"] == latest_period),
                       key=lambda row: (-row["stk_mkv_ratio"], row["symbol"]))
    return {"fund": fund, "news_day": news_day.isoformat(),
            "status": "reported_positions_only", "ann_date": latest_announcement,
            "portfolio_end_date": latest_period, "reported_positions": len(positions),
            "weight_basis": source["weight_basis"], "positions": positions}


def _fetch_namechanges(pro: Any, start: date, end: date) -> list[dict[str, Any]]:
    """Fetch announcement-dated names without accepting a capped API response."""
    frame = pro.namechange(start_date=start.strftime("%Y%m%d"),
                           end_date=end.strftime("%Y%m%d"))
    if len(frame) >= NAMECHANGE_ROW_CAP:
        if start == end:
            raise ValueError(f"namechange capped on {start}")
        midpoint = start + timedelta(days=(end - start).days // 2)
        return (_fetch_namechanges(pro, start, midpoint)
                + _fetch_namechanges(pro, midpoint + timedelta(days=1), end))
    if not set(NAME_FIELDS) <= set(frame.columns):
        raise ValueError("namechange fields missing")
    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict("records"):
        announced = _date(raw["ann_date"])
        effective = _date(raw["start_date"])
        ended = _date(raw["end_date"]) if raw["end_date"] is not None else None
        if (not start <= announced <= end or effective < announced
                or (ended is not None and ended < effective)):
            raise ValueError("invalid namechange interval")
        code = str(raw["ts_code"])
        name = str(raw["name"]).strip()
        if not SECURITY_CODE.fullmatch(code) or not name:
            raise ValueError("invalid namechange code or name")
        rows.append({"ts_code": code, "name": name,
                     "ann_date": announced.isoformat(),
                     "start_date": effective.isoformat(),
                     "end_date": ended.isoformat() if ended else None})
    return rows


def collect_namechanges(pro: Any, output_root: Path,
                        *, start: date, end: date) -> dict[str, Any]:
    """Save historical stock names; never send later names into an earlier replay."""
    if start > end:
        raise ValueError("start after end")
    rows = _fetch_namechanges(pro, start, end)
    unique = {tuple(row[field] for field in NAME_FIELDS) for row in rows}
    if len(unique) != len(rows):
        raise ValueError("duplicate namechange rows")
    rows.sort(key=lambda row: (row["ts_code"], row["start_date"],
                               row["ann_date"], row["name"]))
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / "stock_name_changes.json"
    _atomic_text(path, json.dumps({"source": "Tushare namechange",
                                   "start": start.isoformat(), "end": end.isoformat(),
                                   "rows": rows}, ensure_ascii=False, indent=2) + "\n")
    manifest = {"source": "Tushare namechange", "start": start.isoformat(),
                "end": end.isoformat(), "rows": len(rows), "sha256": _sha(path),
                "file": str(path.resolve()),
                "availability_rule": "ann_date and start_date must be no later than news day"}
    _atomic_text(output_root / "stock_name_changes_manifest.json",
                 json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def stock_names_as_of(output_root: Path, stocks: set[str],
                      news_day: date) -> dict[str, dict[str, Any]]:
    """Resolve many holdings with one verified read of the dated name source."""
    manifest = json.loads((output_root / "stock_name_changes_manifest.json").read_text(
        encoding="utf-8"))
    path = Path(manifest["file"])
    if _sha(path) != manifest["sha256"]:
        raise ValueError("namechange source digest mismatch")
    source = json.loads(path.read_text(encoding="utf-8"))
    matches: dict[str, list[dict[str, Any]]] = {stock: [] for stock in stocks}
    day = news_day.isoformat()
    for row in source["rows"]:
        code = row["ts_code"]
        if (code in matches and row["ann_date"] <= day and row["start_date"] <= day
                and (row["end_date"] is None or day <= row["end_date"])):
            matches[code].append(row)
    result: dict[str, dict[str, Any]] = {}
    for stock, candidates in matches.items():
        if len(candidates) != 1:
            result[stock] = {"stock": stock, "news_day": day,
                             "status": "unknown" if not candidates else "ambiguous"}
            continue
        row = candidates[0]
        result[stock] = {"stock": stock, "news_day": day,
                         "status": "verified", "name": row["name"],
                         "ann_date": row["ann_date"], "start_date": row["start_date"]}
    return result


def stock_name_as_of(output_root: Path, stock: str, news_day: date) -> dict[str, Any]:
    """Resolve one holding to its announced name on the historical news day."""
    return stock_names_as_of(output_root, {stock}, news_day)[stock]


def holding_mentions_as_of(output_root: Path, news_day: date,
                           headlines: dict[str, str],
                           issuer_symbols: dict[str, str] | None = None
                           ) -> dict[str, list[dict[str, Any]]]:
    """Find headline names or exact announcement issuers in prior holdings.

    This is a positive evidence index. A missing match says nothing about an
    ETF's complete basket or whether an event benefits its other components.
    """
    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    reported: list[tuple[str, dict[str, Any], int, dict[str, Any]]] = []
    for fund in sorted(manifest["files"]):
        exposure = exposure_as_of(output_root, fund, news_day)
        if exposure["status"] != "reported_positions_only":
            continue
        for rank, position in enumerate(exposure["positions"], 1):
            reported.append((fund, exposure, rank, position))
    names = stock_names_as_of(output_root,
                              {position["symbol"] for _, _, _, position in reported},
                              news_day)
    matches: dict[str, list[dict[str, Any]]] = {}
    for record_id, headline in headlines.items():
        findings = []
        for fund, exposure, rank, position in reported:
            name_result = names[position["symbol"]]
            name = name_result.get("name", "")
            issuer_match = (issuer_symbols is not None
                            and issuer_symbols.get(record_id) == position["symbol"])
            title_match = (name_result["status"] == "verified"
                           and len(name) >= 3 and name in headline)
            if not issuer_match and not title_match:
                continue
            findings.append({"fund": fund, "stock": position["symbol"],
                             "stock_name": name, "reported_rank": rank,
                             "match_basis": "announcement_issuer_code" if issuer_match
                                            else "headline_stock_name",
                             "stk_mkv_ratio": position["stk_mkv_ratio"],
                             "weight_basis": exposure["weight_basis"],
                             "portfolio_ann_date": exposure["ann_date"],
                             "portfolio_end_date": exposure["portfolio_end_date"],
                             "stock_name_ann_date": name_result.get("ann_date")})
        if findings:
            matches[record_id] = findings
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise ValueError("下载持仓前请设置 TUSHARE_TOKEN")
    manifest = collect(ts.pro_api(token), args.candidates, args.output_root,
                       start=args.start, end=args.end)
    print(json.dumps({"funds": manifest["funds"], "rows": manifest["rows"],
                      "manifest": str(args.output_root / "manifest.json")}), flush=True)


if __name__ == "__main__":
    main()
