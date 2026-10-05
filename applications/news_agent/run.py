"""Edit config/settings.yaml, then run `python -m news_agent.run`."""

from __future__ import annotations

import json
from datetime import date, timedelta

from .settings import Settings, etf_universe, load_settings
from .evaluation.evaluate import evaluate_year
from .processing.prepare import prepare_day
from .report import write_day_report
from .processing.results import validate_initial_results


def _write_candidates(settings: Settings) -> None:
    universe = etf_universe(settings.etf_config)
    path = settings.output_root / "etf_candidates.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ETF 候选池（全部配置项；名称供理解，输出使用完整代码）\n" +
                    "\n".join(f"{code}\t{name}" for code, name in universe.items()) + "\n", encoding="utf-8")


def main() -> None:
    settings = load_settings()
    if settings.mode in {"prepare", "prepare_year"}:
        _write_candidates(settings)
        if settings.mode == "prepare":
            manifest = prepare_day(settings, settings.day)
            print(json.dumps({"day": settings.day.isoformat(), "raw": manifest["raw_count"],
                              "prepared": manifest["prepared_count"], "batches": len(manifest["batches"]),
                              "initial": validate_initial_results(settings, settings.day)}, ensure_ascii=False))
            return
        day, end = date(settings.year, 1, 1), date(settings.year + 1, 1, 1)
        while day < end:
            manifest = prepare_day(settings, day)
            print(f"{day}: raw={manifest['raw_count']} prepared={manifest['prepared_count']} batches={len(manifest['batches'])}", flush=True)
            day += timedelta(days=1)
    elif settings.mode == "report":
        _write_candidates(settings)
        path = write_day_report(settings, settings.day, etf_universe(settings.etf_config))
        print(path)
    elif settings.mode == "evaluate":
        result = evaluate_year(settings, settings.year)
        print(json.dumps({"status": result["status"], "missing_prepared_days": result.get("missing_prepared_days", 0),
                          "missing_result_days": len(result.get("missing_result_days", []))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
