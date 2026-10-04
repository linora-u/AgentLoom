"""Describe ETF open-to-open thresholds without reading held-out price periods.

This is an unconditional market baseline, not a news-agent backtest. The current
candidate pool is held fixed; overlapping windows and correlated ETFs are not
independent observations. No production settings or news predictions are edited.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyarrow.dataset as ds
import yaml


REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "applications/news_agent/data"
OUTPUT = DATA / "evaluation/research/20261001_thresholds"
# Prices after August are needed only for the final August holding windows.
PRICE_RANGES = (("20240101", "20250110"), ("20250801", "20250905"))
PERIODS = (("2024", "20240101", "20241231"),
           ("2025-08-development", "20250801", "20250831"))
EXPLICIT_CROSS_BORDER = {"159699", "159659", "159506", "159131", "159726", "159302"}


def category(symbol: str) -> str:
    if symbol == "517520":
        return "沪深港黄金产业股票"
    if symbol.startswith(("513", "520")) or symbol in EXPLICIT_CROSS_BORDER:
        return "港股及海外股票"
    return "A股股票"


def read_prices(path: Path) -> dict[str, dict]:
    dataset = ds.dataset(path, format="parquet")
    field = ds.field("trade_date")
    filters = None
    for start, end in PRICE_RANGES:
        term = (field >= start) & (field <= end)
        filters = term if filters is None else filters | term
    rows = dataset.to_table(columns=["trade_date", "open", "close", "adj_factor", "vol"],
                            filter=filters).to_pylist()
    result = {row["trade_date"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate trade dates: {path}")
    return result


def valid_price(row: dict, *, require_close: bool) -> bool:
    fields = ["open", "adj_factor", "vol"] + (["close"] if require_close else [])
    return all(isinstance(row.get(key), (int, float)) and math.isfinite(row[key])
               and row[key] > 0 for key in fields)


def stats(rows: list[dict]) -> dict:
    values = np.array([row["return"] for row in rows], dtype=float)
    if not len(values):
        return {"windows": 0}
    result = {
        "windows": len(rows), "etfs": len({row["code"] for row in rows}),
        "entry_dates": len({row["entry_date"] for row in rows}),
        "mean_return": float(values.mean()), "median_return": float(np.median(values)),
        "std_return": float(values.std(ddof=1)) if len(values) > 1 else None,
        "median_absolute_return": float(np.median(abs(values))),
        "positive_share": float(np.mean(values > 0)),
    }
    for threshold in (.01, .015, .02, .025):
        key = f"{threshold:.3f}"
        result[f"up_ge_{key}"] = float(np.mean(values >= threshold - 1e-12))
        result[f"down_le_-{key}"] = float(np.mean(values <= -threshold + 1e-12))
    result["new_over_old_up_frequency"] = (result["up_ge_0.020"] / result["up_ge_0.010"]
                                             if result["up_ge_0.010"] else None)
    result["new_over_old_down_frequency"] = (result["down_le_-0.015"] / result["down_le_-0.010"]
                                              if result["down_le_-0.010"] else None)
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    config_path = DATA / "etf_universe.yaml"
    universe = yaml.safe_load(config_path.read_text())["etf_dk_merge"]["benchmarks"]
    root = DATA / "etf_dk_merge"
    calendar_path = root / "ts_code=510300.SH/data.parquet"
    calendar = sorted(read_prices(calendar_path))
    returns: list[dict] = []
    coverage: list[dict] = []
    hashes: dict[str, str] = {}
    for item in universe:
        symbol = str(item["target_symbol"])
        code = symbol + (".SH" if symbol.startswith("5") else ".SZ")
        path = root / f"ts_code={code}" / "data.parquet"
        if not path.is_file():
            coverage.append({"code": code, "period": "all", "reason": "missing_file"})
            continue
        hashes[code] = hashlib.sha256(path.read_bytes()).hexdigest()
        prices = read_prices(path)
        for period, start, end in PERIODS:
            counts: Counter = Counter()
            for index, entry_date in enumerate(calendar):
                if not start <= entry_date <= end:
                    continue
                counts["calendar_entry_dates"] += 1
                if index + 3 >= len(calendar):
                    counts["insufficient_calendar"] += 1
                    continue
                dates = calendar[index:index + 4]
                if any(day not in prices for day in dates):
                    counts["missing_price_date"] += 1
                    continue
                if any(not valid_price(prices[day], require_close=(offset < 3))
                       for offset, day in enumerate(dates)):
                    counts["invalid_price_or_volume"] += 1
                    continue
                first, last = prices[dates[0]], prices[dates[-1]]
                realized = last["open"] * last["adj_factor"] / (first["open"] * first["adj_factor"]) - 1
                returns.append({"period": period, "code": code, "name": item["name"],
                                "category": category(symbol), "entry_date": dates[0],
                                "exit_date": dates[-1], "return": realized})
                counts["valid_windows"] += 1
            coverage.append({"period": period, "code": code, "name": item["name"],
                             "category": category(symbol), **counts})
    grouped: dict[tuple, list] = defaultdict(list)
    for row in returns:
        grouped[(row["period"], row["category"])].append(row)
    summaries = [{"period": period, "category": group, **stats(rows)}
                 for (period, group), rows in grouped.items()]
    by_etf: dict[tuple, list] = defaultdict(list)
    by_month: dict[tuple, list] = defaultdict(list)
    for row in returns:
        by_etf[(row["period"], row["code"], row["name"], row["category"])].append(row)
        if row["category"] == "A股股票":
            by_month[(row["period"], row["entry_date"][:6])].append(row)
    per_etf = [{"period": period, "code": code, "name": name, "category": group, **stats(rows)}
               for (period, code, name, group), rows in by_etf.items()]
    per_month = [{"period": period, "entry_month": month, **stats(rows)}
                 for (period, month), rows in sorted(by_month.items())]
    # A sensitivity check uses disjoint three-session blocks anchored to the
    # first entry date of each period, rather than treating overlaps as new data.
    nonoverlap = []
    for period, start, end in PERIODS:
        entry_dates = [day for day in calendar if start <= day <= end][::3]
        rows = [row for row in returns if row["period"] == period
                and row["category"] == "A股股票" and row["entry_date"] in entry_dates]
        nonoverlap.append({"period": period, **stats(rows)})
    result = {
        "as_of": "2026-10-01", "holding_sessions": 3, "entry": "adjusted_open",
        "exit": "adjusted_open_T+3", "universe_count": len(universe),
        "universe_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "price_filters": PRICE_RANGES, "periods": PERIODS, "summaries": summaries,
        "nonoverlap_A_share": nonoverlap, "per_etf": per_etf, "per_month_A_share": per_month,
        "coverage": coverage, "source_hashes": hashes,
        "limitations": ["Unconditional price frequencies, not Agent prediction win rates",
                        "Current candidate pool, not a point-in-time full-market universe",
                        "Overlapping windows and correlated ETF exposures",
                        "Fees/slippage omitted; ideal adjusted opening-price convention",
                        "No held-out October-December 2025 returns computed",
                        "Commodity-themed equity ETFs are not commodity spot ETFs"],
    }
    (OUTPUT / "distribution.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    write_csv(OUTPUT / "windows.csv", returns)
    write_csv(OUTPUT / "etf_summary.csv", per_etf)
    write_csv(OUTPUT / "month_summary.csv", per_month)
    write_csv(OUTPUT / "coverage.csv", coverage)
    representative = {"510300.SH", "510500.SH", "588000.SH", "159995.SZ", "515880.SH",
                      "512880.SH", "159887.SZ", "512890.SH", "159915.SZ"}
    print(json.dumps({"output": str(OUTPUT), "summaries": summaries,
                      "representative": [row for row in per_etf if row["code"] in representative],
                      "month_summary_A_share": per_month, "nonoverlap": nonoverlap},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
