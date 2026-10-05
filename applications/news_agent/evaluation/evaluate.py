from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from ..settings import Settings
from ..report import write_year_report


def _date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10]) if "-" in str(value) else datetime.strptime(str(value), "%Y%m%d").date()


def _valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


class PriceStore:
    """Use a liquid listed A-share ETF as the local exchange-session calendar."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache: dict[str, dict[date, dict[str, Any]]] = {}
        reference = self.prices("510300.SH")
        if not reference:
            raise FileNotFoundError("缺少 510300.SH 行情，无法确定本地 A 股交易日")
        self.calendar = sorted(reference)
        if not self.calendar:
            raise ValueError("510300.SH 缺少有效交易日")

    def prices(self, code: str) -> dict[date, dict[str, Any]]:
        if code not in self.cache:
            path = self.root / f"ts_code={code}" / "data.parquet"
            if not path.exists():
                self.cache[code] = {}
            else:
                parquet = pq.ParquetFile(path)
                needed = [name for name in ("trade_date", "open", "close", "vol", "adj_factor") if name in parquet.schema_arrow.names]
                if set(needed) != {"trade_date", "open", "close", "vol", "adj_factor"}:
                    raise ValueError(f"ETF 行情缺字段: {path}")
                rows = parquet.read(columns=needed).to_pylist()
                by_day = {_date(row["trade_date"]): row for row in rows}
                if len(by_day) != len(rows):
                    raise ValueError(f"ETF 行情存在重复交易日: {path}")
                self.cache[code] = by_day
        return self.cache[code]



def evaluate_year(settings: Settings, year: int) -> dict[str, Any]:
    """Use the same verified three-day open-to-open contract as monthly scoring."""
    from ..settings import load_baseline_settings
    from .baseline import verify_evaluation_artifacts
    from .period import dates, score

    config = load_baseline_settings()
    start, end = date(year, 1, 1), date(year, 12, 31)
    roots = {}
    for month in range(1, 13):
        key = f"{year}-{month:02d}"
        version_month = settings.output_root / config.version / key
        roots[key] = version_month
    missing = []
    for day in dates(start, end):
        try:
            verify_evaluation_artifacts(roots[day.isoformat()[:7]], day)
        except (FileNotFoundError, ValueError) as error:
            missing.append({"day": str(day), "reason": str(error)})
    if missing:
        result = {"year": year, "status": "missing_agent_results",
                  "missing_result_days": missing,
                  "missing_prepared_days": sum(not (roots[item["day"][:7]] / "days" / item["day"] / "manifest.json").is_file()
                                                for item in missing)}
    else:
        result = {"year": year, "status": "complete", "independent_validation": False,
                  **score(roots, start, end, entry_dates=[str(day) for day in PriceStore(settings.etf_root).calendar if day.year == year])}
    write_year_report(settings.output_root / "reports" / f"{year}.md", result)
    return result
