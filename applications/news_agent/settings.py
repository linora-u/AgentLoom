from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "news_agent" / "config" / "settings.yaml"
LEVEL_THRESHOLD_KEYS = ("positive", "negative")


@dataclass(frozen=True)
class Settings:
    news_root: Path
    etf_root: Path
    etf_config: Path
    output_root: Path
    mode: str
    day: date
    year: int
    initial_max_tokens: int | None = None
    token_encoding: str = "o200k_base"
    timezone: str = "Asia/Shanghai"
    refined_max_tokens: int = 160000


@dataclass(frozen=True)
class BaselineSettings:
    evaluation_root: Path
    prices: Path
    candidates: Path
    initial_workers: int
    refined_workers: int
    initial_max_tokens: int
    refined_max_tokens: int
    token_encoding: str
    hold_days: int
    sell_at: str
    thresholds: dict[str, Decimal]
    exposure_root: Path | None = None
    version: str = ""
    development_month: str | None = None
    validation_months: tuple[str, ...] = ()


def _read_config(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置不是映射: {path}")
    return raw


def _project_path(raw: dict, key: str) -> Path:
    value = Path(raw[key]).expanduser()
    return value if value.is_absolute() else ROOT / value


def _batching(raw: dict) -> dict:
    import tiktoken

    section = raw.get("batching")
    if not isinstance(section, dict) or set(section) != {
            "token_encoding", "initial_max_tokens", "refined_max_tokens"}:
        raise ValueError("batching 必须包含 token_encoding、initial_max_tokens 和 refined_max_tokens")
    for key in ("initial_max_tokens", "refined_max_tokens"):
        if type(section[key]) is not int or section[key] < 100:
            raise ValueError(f"batching.{key} 必须是至少为 100 的整数")
    if not isinstance(section["token_encoding"], str):
        raise ValueError("batching.token_encoding 必须是分词编码名称")
    tiktoken.get_encoding(section["token_encoding"])
    return section


def validate_baseline_prediction(hold_days: int, sell_at: str,
                                 thresholds: Mapping[str, Decimal]) -> None:
    if type(hold_days) is not int or hold_days < 1:
        raise ValueError("持有交易日必须大于零")
    if sell_at not in {"open", "close"}:
        raise ValueError("卖出时点只能是 open 或 close")
    if (set(thresholds) != set(LEVEL_THRESHOLD_KEYS)
            or not all(value.is_finite() for value in thresholds.values())
            or not (thresholds["negative"] < 0 < thresholds["positive"])):
        raise ValueError("收益阈值必须满足 negative < 0 < positive")


def load_baseline_settings(path: Path = CONFIG_PATH) -> BaselineSettings:
    raw = _read_config(path)
    section = raw.get("baseline")
    if not isinstance(section, dict):
        raise ValueError("baseline 配置必须是映射")

    def positive_int(key: str) -> int:
        value = section.get(key)
        if type(value) is not int or value < 1:
            raise ValueError(f"baseline.{key} 必须是正整数")
        return value

    thresholds_raw = section.get("thresholds")
    if not isinstance(thresholds_raw, dict) or set(thresholds_raw) != set(LEVEL_THRESHOLD_KEYS):
        raise ValueError("baseline.thresholds 必须包含 positive 和 negative 两个收益阈值")
    try:
        thresholds = {key: Decimal(str(thresholds_raw[key])) for key in LEVEL_THRESHOLD_KEYS}
    except InvalidOperation as error:
        raise ValueError("baseline.thresholds 必须是十进制数字") from error
    sell_at = section.get("sell_at")
    if not isinstance(sell_at, str):
        raise ValueError("baseline.sell_at 只能是 open 或 close")
    hold_days = positive_int("hold_days")
    validate_baseline_prediction(hold_days, sell_at, thresholds)
    evaluation_root = _project_path(raw, "evaluation_output_root")
    exposure_value = section.get("exposure_root")
    if exposure_value is not None and (not isinstance(exposure_value, str)
                                       or not exposure_value.strip()):
        raise ValueError("baseline.exposure_root 必须是非空路径")
    exposure_root = (None if exposure_value is None else
                     Path(exposure_value).expanduser())
    if exposure_root is not None and not exposure_root.is_absolute():
        exposure_root = ROOT / exposure_root
    batching = _batching(raw)
    replay = section.get("replay", {})
    if not isinstance(replay, dict):
        raise ValueError("baseline.replay 必须是映射")
    version = replay.get("version", "")
    development_month = replay.get("development_month")
    validation_months = replay.get("validation_months", [])
    if not isinstance(version, str) or (version and not re.fullmatch(r"[A-Za-z0-9_-]+", version)):
        raise ValueError("baseline.replay.version 必须是安全的目录名称")
    if not isinstance(validation_months, list) or any(not isinstance(month, str) for month in validation_months):
        raise ValueError("baseline.replay.validation_months 必须是月份列表")
    months = ([development_month] if development_month is not None else []) + validation_months
    if any(not isinstance(month, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month) for month in months):
        raise ValueError("回放月份必须为 YYYY-MM")
    if len(set(months)) != len(months) or (development_month and any(month <= development_month for month in validation_months)):
        raise ValueError("验收月份必须晚于开发月份，且不能重复")
    month_numbers = [int(month[:4]) * 12 + int(month[5:]) for month in validation_months]
    if any(right != left + 1 for left, right in zip(month_numbers, month_numbers[1:])):
        raise ValueError("验收月份必须按时间连续排列")
    return BaselineSettings(
        evaluation_root=evaluation_root,
        prices=_project_path(raw, "etf_root"),
        candidates=evaluation_root / "etf_candidates.txt",
        initial_workers=positive_int("initial_workers"),
        refined_workers=positive_int("refined_workers"),
        initial_max_tokens=batching["initial_max_tokens"],
        refined_max_tokens=batching["refined_max_tokens"],
        token_encoding=batching["token_encoding"],
        hold_days=hold_days,
        sell_at=sell_at,
        thresholds=thresholds,
        exposure_root=exposure_root,
        version=version,
        development_month=development_month,
        validation_months=tuple(validation_months),
    )


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    raw = _read_config(path)

    def project_path(key: str) -> Path:
        return _project_path(raw, key)

    timezone = str(raw.get("timezone", "Asia/Shanghai"))
    if timezone != "Asia/Shanghai":
        raise ValueError("首版只支持 Asia/Shanghai 时区")
    day_value = raw.get("day", "latest")
    if day_value == "latest":
        summary = project_path("news_root") / "_manifest" / "download_summary.json"
        if not summary.exists():
            raise ValueError(f"无法确定最近已下载日期: {summary}")
        cutoff = json.loads(summary.read_text(encoding="utf-8")).get("cutoff_date")
        if not cutoff:
            raise ValueError(f"下载摘要没有截止日期: {summary}")
        day = date.fromisoformat(cutoff)
    elif day_value == "today":
        day = datetime.now(ZoneInfo(timezone)).date()
    else:
        day = date.fromisoformat(str(day_value))
    mode = str(raw["mode"])
    scope = str(raw.get("scope", "daily"))
    if scope not in {"daily", "evaluation"}:
        raise ValueError(f"未知数据范围: {scope}")
    output_key = "evaluation_output_root" if mode in {"prepare_year", "evaluate"} or scope == "evaluation" else "daily_output_root"
    batching = _batching(raw)
    settings = Settings(
        news_root=project_path("news_root"),
        etf_root=project_path("etf_root"),
        etf_config=project_path("etf_config"),
        output_root=project_path(output_key),
        initial_max_tokens=batching["initial_max_tokens"],
        refined_max_tokens=batching["refined_max_tokens"],
        token_encoding=batching["token_encoding"],
        mode=mode,
        day=day,
        year=int(raw["year"]),
        timezone=timezone,
    )
    if settings.mode not in {"prepare", "prepare_year", "report", "evaluate"}:
        raise ValueError(f"未知模式: {settings.mode}")
    return settings


def etf_universe(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    benchmarks = raw["etf_dk_merge"]["benchmarks"]
    universe: dict[str, str] = {}
    for item in benchmarks:
        symbol = str(item["target_symbol"])
        if "." in symbol:
            code = symbol
        else:
            code = f"{symbol}.SH" if symbol.startswith(("5", "6")) else f"{symbol}.SZ"
        if code in universe:
            raise ValueError(f"ETF 配置重复: {code}")
        universe[code] = str(item["name"])
    if not universe:
        raise ValueError("ETF 配置为空")
    return universe
