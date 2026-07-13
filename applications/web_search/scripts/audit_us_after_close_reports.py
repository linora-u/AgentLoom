from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

NEW_YORK_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")
REQUIRED_COLUMNS = [
    "排名",
    "隔夜企业",
    "隔夜涨跌",
    "对应A股行业板块",
    "板块内利好判断",
    "分数",
    "依据",
    "证据时间",
    "URL",
    "利好持续周期",
]
SECTOR_REQUIRED_COLUMNS = [
    "板块排名",
    "美股板块/主题",
    "隔夜表现",
    "代表企业/证据",
    "对应A股行业板块",
    "A股影响判断",
    "分数",
    "依据",
    "证据时间",
    "URL",
    "影响周期",
]
EXPECTED_RANKS = ["涨1", "涨2", "涨3", "跌1", "跌2", "跌3"]
EXPECTED_SECTOR_RANKS = ["领涨板块1", "领涨板块2", "领跌板块1", "领跌板块2"]
SOURCE_TAG_RE = re.compile(r"\bT\d+\b")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
URL_RE = re.compile(r"https?://")
SCORE_RE = re.compile(r"^[+-]?\d+(?:\.\d)?$")
WINDOW_END_RE = re.compile(r"窗口：.*?~\s*(?P<end>[^；\n]+)")
TIMESTAMP_WITH_TIME_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})(?:T| )"
    r"(?P<time>\d{2}:\d{2}(?::\d{2})?)"
    r"(?P<tz>Z|[+-]\d{2}:\d{2}|\s*(?:ET|UTC))?"
)
FORBIDDEN_INDUSTRY_PATTERNS = [
    "美股",
    "ETF",
    "指数",
    "市场映射",
    "市场整体",
    "大盘",
    "资金流",
    "被动资金",
]
FORBIDDEN_REPORT_PATTERNS = [
    "| 行业 | 方向 | 分数 | 事件 |",
    "上涨主表",
    "下跌主表",
    "达交易阈值",
    "±24",
    "Market Regime",
    "DriverEvidence 验证表",
    "D4 自检",
    "因果质量审计",
]
NO_POSITIVE_MARKERS = ("无新增利好", "无利好", "有利空", "兑现风险", "无法映射")
ZERO_SCORE_MARKERS = ("无新增利好", "无利好", "无法映射")
NO_CYCLE_MARKERS = ("无", "不适用", "仅风险", "非利好")


@dataclass
class AuditIssue:
    severity: str
    message: str
    row: int | None = None


@dataclass
class ReportAudit:
    path: str
    row_count: int
    issues: list[AuditIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


def _split_table_row(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _parse_table(text: str) -> tuple[list[str], list[dict[str, str]], list[AuditIssue]]:
    issues: list[AuditIssue] = []
    lines = text.splitlines()
    header_index = None

    for index, line in enumerate(lines):
        if line.strip().startswith("| 排名 | 隔夜企业 | 隔夜涨跌 | 对应A股行业板块 |"):
            header_index = index
            break

    if header_index is None:
        return [], [], [AuditIssue("error", "missing required overnight company table header")]

    headers = _split_table_row(lines[header_index])
    if headers != REQUIRED_COLUMNS:
        issues.append(AuditIssue("error", f"unexpected columns: {headers}"))

    rows: list[dict[str, str]] = []
    for raw_index, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = _split_table_row(stripped)
        if len(cells) != len(REQUIRED_COLUMNS):
            issues.append(AuditIssue("error", f"row has {len(cells)} cells, expected {len(REQUIRED_COLUMNS)}", raw_index))
            continue
        rows.append(dict(zip(REQUIRED_COLUMNS, cells, strict=True)))

    if len(rows) != 6:
        issues.append(AuditIssue("error", f"table must contain exactly 6 rows, got {len(rows)}"))

    return headers, rows, issues


def _parse_sector_table(text: str) -> tuple[list[str], list[dict[str, str]], list[AuditIssue]]:
    issues: list[AuditIssue] = []
    lines = text.splitlines()
    header_index = None

    for index, line in enumerate(lines):
        if line.strip().startswith("| 板块排名 | 美股板块/主题 |"):
            header_index = index
            break

    if header_index is None:
        return [], [], [AuditIssue("error", "missing required sector leader/laggard table header")]

    headers = _split_table_row(lines[header_index])
    if headers != SECTOR_REQUIRED_COLUMNS:
        issues.append(AuditIssue("error", f"unexpected sector columns: {headers}"))

    rows: list[dict[str, str]] = []
    for raw_index, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = _split_table_row(stripped)
        if len(cells) != len(SECTOR_REQUIRED_COLUMNS):
            issues.append(
                AuditIssue(
                    "error",
                    f"sector row has {len(cells)} cells, expected {len(SECTOR_REQUIRED_COLUMNS)}",
                    raw_index,
                )
            )
            continue
        rows.append(dict(zip(SECTOR_REQUIRED_COLUMNS, cells, strict=True)))

    if len(rows) != 4:
        issues.append(AuditIssue("error", f"sector table must contain exactly 4 rows, got {len(rows)}"))

    return headers, rows, issues


def _source_tags_from_time(value: str) -> list[str]:
    return SOURCE_TAG_RE.findall(value)


def _source_tags_from_url(value: str) -> list[str]:
    return [match.group(1).strip() for match in MARKDOWN_LINK_RE.finditer(value)]


def _hosts_from_url(value: str) -> list[str]:
    hosts: list[str] = []
    for match in MARKDOWN_LINK_RE.finditer(value):
        hostname = urlparse(match.group(2)).hostname
        if hostname:
            hosts.append(hostname.rstrip(".").lower())
    return hosts


def _is_example_host(host: str) -> bool:
    return host == "example.com" or host.endswith(".example.com")


def _has_duplicates(values: list[str]) -> bool:
    return len(values) != len(set(values))


def _normalized_basis(value: str) -> str:
    return re.sub(r"\s+", "", value.lower())


def _parse_score(value: str) -> float | None:
    if not SCORE_RE.fullmatch(value.strip()):
        return None
    return float(value)


def _validate_status_score_cycle(
    issues: list[AuditIssue],
    status: str,
    score_text: str,
    cycle: str,
    row: int,
    *,
    score_label: str = "score",
) -> None:
    if not any(marker in status for marker in ("有利好", "无新增利好", "无利好", "有利空", "兑现风险", "无法映射")):
        issues.append(AuditIssue("error", f"invalid sector-positive status: {status}", row))

    score = _parse_score(score_text)
    if score is None:
        issues.append(AuditIssue("error", f"invalid {score_label} format: {score_text}", row))
    else:
        if score < -40.0 or score > 40.0:
            issues.append(AuditIssue("error", f"{score_label} out of range [-40, 40]: {score_text}", row))
        if "有利好" in status and score <= 0:
            issues.append(AuditIssue("error", "positive sector status must have a positive score", row))
        if "有利空" in status and score >= 0:
            issues.append(AuditIssue("error", "negative sector status must have a negative score", row))
        if any(marker in status for marker in ZERO_SCORE_MARKERS) and abs(score) > 1e-9:
            issues.append(AuditIssue("error", "non-catalyst sector status must have a 0.0 score", row))
        if "兑现风险" in status and score > 0:
            issues.append(AuditIssue("error", "profit-taking risk must not have a positive score", row))

    if any(marker in status for marker in NO_POSITIVE_MARKERS) and not any(marker in cycle for marker in NO_CYCLE_MARKERS):
        issues.append(AuditIssue("error", "non-positive sector status must not claim a positive duration", row))
    if "有利好" in status and any(marker in cycle for marker in ("无", "不适用")):
        issues.append(AuditIssue("error", "positive sector status must include a concrete positive duration", row))


def _validate_sources(
    issues: list[AuditIssue],
    evidence_time: str,
    url: str,
    cutoff: datetime | None,
    row: int,
) -> None:
    time_tags = _source_tags_from_time(evidence_time)
    url_tags = _source_tags_from_url(url)
    if not evidence_time or evidence_time == "未披露":
        issues.append(AuditIssue("warning", "evidence time is missing or undisclosed", row))
    evidence_timestamps = _timestamps_from_value(evidence_time)
    if time_tags and len(evidence_timestamps) < len(time_tags):
        issues.append(AuditIssue("error", "each evidence source must include hour/minute and timezone", row))
    if cutoff is not None:
        for evidence_timestamp in evidence_timestamps:
            if evidence_timestamp.astimezone(UTC_TZ) > cutoff.astimezone(UTC_TZ):
                issues.append(
                    AuditIssue(
                        "error",
                        f"evidence time exceeds news cutoff: {evidence_timestamp.isoformat()} > {cutoff.isoformat()}",
                        row,
                    )
                )
    if not URL_RE.search(url) or not MARKDOWN_LINK_RE.search(url):
        issues.append(AuditIssue("error", f"missing markdown URL: {url}", row))
    if not time_tags:
        issues.append(AuditIssue("error", "evidence time must include source tag like T1", row))
    if not url_tags:
        issues.append(AuditIssue("error", "URL links must use source tags like [T1](url)", row))
    if any(not SOURCE_TAG_RE.fullmatch(tag) for tag in url_tags):
        issues.append(AuditIssue("error", f"URL link labels must be source tags T1/T2: {url}", row))
    if _has_duplicates(time_tags):
        issues.append(AuditIssue("error", f"duplicate evidence time source tags: {evidence_time}", row))
    if _has_duplicates(url_tags):
        issues.append(AuditIssue("error", f"duplicate URL source tags: {url}", row))
    if set(time_tags) != set(url_tags):
        issues.append(
            AuditIssue(
                "error",
                f"evidence time tags must match URL tags: time={time_tags}, url={url_tags}",
                row,
            )
        )
    if any(_is_example_host(host) for host in _hosts_from_url(url)):
        issues.append(AuditIssue("warning", "example.com URL is only suitable for tests", row))


def _parse_timestamp_match(match: re.Match[str]) -> datetime | None:
    tz_label = (match.group("tz") or "").strip()
    if not tz_label:
        return None

    value = f"{match.group('date')}T{match.group('time')}"
    if tz_label == "Z":
        return datetime.fromisoformat(f"{value}+00:00")
    if tz_label == "UTC":
        return datetime.fromisoformat(value).replace(tzinfo=UTC_TZ)
    if tz_label == "ET":
        return datetime.fromisoformat(value).replace(tzinfo=NEW_YORK_TZ)
    return datetime.fromisoformat(f"{value}{tz_label}")


def _window_end_timestamp(text: str) -> datetime | None:
    match = WINDOW_END_RE.search(text)
    if not match:
        return None
    timestamp_match = TIMESTAMP_WITH_TIME_RE.search(match.group("end"))
    if not timestamp_match:
        return None
    return _parse_timestamp_match(timestamp_match)


def _timestamps_from_value(value: str) -> list[datetime]:
    timestamps: list[datetime] = []
    for match in TIMESTAMP_WITH_TIME_RE.finditer(value):
        parsed = _parse_timestamp_match(match)
        if parsed is not None:
            timestamps.append(parsed)
    return timestamps


def audit_report(path: Path) -> ReportAudit:
    text = path.read_text(encoding="utf-8")
    issues: list[AuditIssue] = []
    cutoff = _window_end_timestamp(text)

    if not text.startswith("# 美股收盘后隔夜重点企业行业映射 - "):
        issues.append(AuditIssue("error", "unexpected title"))

    for pattern in FORBIDDEN_REPORT_PATTERNS:
        if pattern in text:
            issues.append(AuditIssue("error", f"forbidden old scoring report text: {pattern}"))

    for raw_line in text.splitlines():
        stripped = raw_line.strip().strip("*")
        if stripped.startswith("结论") and len(stripped) > 90:
            issues.append(AuditIssue("error", f"summary line is too long: {stripped[:30]}..."))
        if stripped.startswith("主要剔除原因"):
            reason = stripped.split("：", 1)[-1]
            if len(stripped) > 55:
                issues.append(AuditIssue("error", f"rejection reason is too long: {stripped[:30]}..."))
            if "；" in reason:
                issues.append(AuditIssue("error", "rejection reason must not chain multiple reasons with semicolons"))

    _, rows, table_issues = _parse_table(text)
    issues.extend(table_issues)
    _, sector_rows, sector_table_issues = _parse_sector_table(text)
    issues.extend(sector_table_issues)

    ranks = [row["排名"] for row in rows]
    if ranks != EXPECTED_RANKS:
        issues.append(AuditIssue("error", f"ranks must be exactly {EXPECTED_RANKS}, got {ranks}"))

    companies: list[str] = []
    basis_values: list[str] = []
    for index, row in enumerate(rows, start=1):
        rank = row["排名"]
        company = row["隔夜企业"]
        overnight_move = row["隔夜涨跌"]
        industry = row["对应A股行业板块"]
        positive_status = row["板块内利好判断"]
        score_text = row["分数"]
        basis = row["依据"]
        evidence_time = row["证据时间"]
        url = row["URL"]
        positive_cycle = row["利好持续周期"]

        if not company or company in {"-", "无"}:
            issues.append(AuditIssue("error", "missing overnight company", index))
        companies.append(company.lower())

        if rank.startswith("涨") and not any(marker in overnight_move for marker in ("+", "涨", "up", "gain")):
            issues.append(AuditIssue("error", "gain row must show an upward overnight move", index))
        if rank.startswith("跌") and not any(marker in overnight_move for marker in ("-", "跌", "down", "loss")):
            issues.append(AuditIssue("error", "loss row must show a downward overnight move", index))

        if not industry or any(pattern in industry for pattern in FORBIDDEN_INDUSTRY_PATTERNS):
            issues.append(AuditIssue("error", f"industry is not an A-share industry/chain: {industry}", index))

        _validate_status_score_cycle(issues, positive_status, score_text, positive_cycle, index)

        if not basis or len(basis) < 8:
            issues.append(AuditIssue("error", "basis is too thin", index))
        basis_values.append(_normalized_basis(basis))

        _validate_sources(issues, evidence_time, url, cutoff, index)

    if _has_duplicates(companies):
        issues.append(AuditIssue("error", "overnight companies must be unique"))
    if _has_duplicates(basis_values):
        issues.append(AuditIssue("error", "basis rows must not duplicate the same bullish/bearish wording"))

    sector_ranks = [row["板块排名"] for row in sector_rows]
    if sector_ranks != EXPECTED_SECTOR_RANKS:
        issues.append(AuditIssue("error", f"sector ranks must be exactly {EXPECTED_SECTOR_RANKS}, got {sector_ranks}"))

    sector_themes: list[str] = []
    for index, row in enumerate(sector_rows, start=1):
        rank = row["板块排名"]
        theme = row["美股板块/主题"]
        move = row["隔夜表现"]
        evidence = row["代表企业/证据"]
        industry = row["对应A股行业板块"]
        status = row["A股影响判断"]
        score_text = row["分数"]
        basis = row["依据"]
        evidence_time = row["证据时间"]
        url = row["URL"]
        cycle = row["影响周期"]

        if not theme or theme in {"-", "无"}:
            issues.append(AuditIssue("error", "missing sector theme", index))
        sector_themes.append(theme.lower())
        if rank.startswith("领涨") and not any(marker in move for marker in ("+", "涨", "领涨", "up", "gain")):
            issues.append(AuditIssue("error", "sector leader row must show an upward move", index))
        if rank.startswith("领跌") and not any(marker in move for marker in ("-", "跌", "领跌", "down", "loss")):
            issues.append(AuditIssue("error", "sector laggard row must show a downward move", index))
        if not evidence or len(evidence) < 4:
            issues.append(AuditIssue("error", "sector representative evidence is too thin", index))
        if not industry or any(pattern in industry for pattern in FORBIDDEN_INDUSTRY_PATTERNS):
            issues.append(AuditIssue("error", f"sector industry is not an A-share industry/chain: {industry}", index))
        if not basis or len(basis) < 8:
            issues.append(AuditIssue("error", "sector basis is too thin", index))

        _validate_status_score_cycle(issues, status, score_text, cycle, index, score_label="sector score")
        _validate_sources(issues, evidence_time, url, cutoff, index)

    if _has_duplicates(sector_themes):
        issues.append(AuditIssue("error", "sector themes must be unique"))

    return ReportAudit(str(path), len(rows), issues)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    audits = [audit_report(path) for path in args.paths]
    payload = [
        {
            **asdict(audit),
            "ok": audit.ok,
            "issues": [asdict(issue) for issue in audit.issues],
        }
        for audit in audits
    ]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for audit in audits:
            status = "PASS" if audit.ok else "FAIL"
            print(f"{status} {audit.path} rows={audit.row_count}")
            for issue in audit.issues:
                row = f" row={issue.row}" if issue.row is not None else ""
                print(f"  [{issue.severity}]{row} {issue.message}")

    return 0 if all(audit.ok for audit in audits) else 1


if __name__ == "__main__":
    raise SystemExit(main())
