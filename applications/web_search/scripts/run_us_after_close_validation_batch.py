from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from applications.web_search.agent_tools.market_time import get_market_time_context
from applications.web_search.scripts.audit_us_after_close_run_logs import audit_log
from applications.web_search.scripts.audit_us_after_close_reports import audit_report


WORKFLOW = "applications/web_search/workflows/us_after_close_a_share_signal_agent.yaml"
OUTPUT_DIR = REPO_ROOT / "applications/web_search/outputs"
LOG_DIR = OUTPUT_DIR / "validation_logs"
MANIFEST_PATH = OUTPUT_DIR / "validation_us_after_close_batch.json"

DEFAULT_NOWS_UTC = [
    "2026-06-18T00:30:00Z",
    "2026-06-19T00:30:00Z",
    "2026-06-22T00:30:00Z",
    "2026-06-23T00:30:00Z",
    "2026-06-24T00:30:00Z",
    "2026-06-25T00:30:00Z",
    "2026-06-26T00:30:00Z",
    "2026-06-29T00:30:00Z",
    "2026-06-30T00:30:00Z",
    "2026-07-01T00:30:00Z",
    "2026-07-02T11:00:00Z",
]

TASK = """
按 workflow 的搜索优先工具流生成一次报告：先取市场时间，再做最低搜索矩阵，并按 workflow 继续补充搜索/抽取直到证据足够。
最低搜索矩阵是硬前置：即使第一轮 MarketDiscovery 已经搜到榜单，也必须先完成第二轮 DriverDiscovery batch_search；第一次 extract 之前必须已经有两次有效 batch_search。
不要为了省搜索调用牺牲准确性；但 extract 是高成本全文阅读，必须遵守 workflow 的 extra_search_round/extract_count 上限，达到停止条件或计数上限后立刻写报告。
不要使用 workflow 禁止的工具。最终 final_answer 只回复报告路径。
"""


def _context(now_utc: str) -> dict[str, object]:
    return json.loads(get_market_time_context(now_utc))


def _report_path(context: dict[str, object]) -> Path:
    us_day = context["query_terms"]["us_trading_day_iso"]
    a_day = context["query_terms"]["a_share_prediction_date_iso"]
    return OUTPUT_DIR / f"us_after_close_a_share_signal_{us_day}_to_{a_day}.md"


def _safe_stamp(now_utc: str) -> str:
    return now_utc.replace(":", "").replace("-", "").replace("+", "").replace("Z", "Z")


def _run_one(now_utc: str, timeout_seconds: int, skip_existing_ok: bool) -> dict[str, object]:
    context = _context(now_utc)
    report_path = _report_path(context)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{_safe_stamp(now_utc)}.log"

    if skip_existing_ok and report_path.exists():
        audit = audit_report(report_path)
        log_audit = audit_log(log_path) if log_path.exists() else None
        if audit.ok and log_audit is not None and log_audit.ok:
            return {
                "now_utc": now_utc,
                "started_at": None,
                "finished_at": None,
                "returncode": 0,
                "timed_out": False,
                "skipped": True,
                "context": {
                    "us_trading_day": context["query_terms"]["us_trading_day_iso"],
                    "a_share_prediction_date": context["query_terms"]["a_share_prediction_date_iso"],
                    "news_window_start_asia_shanghai": context["news_window"]["start_asia_shanghai"],
                    "news_window_end_asia_shanghai": context["news_window"]["end_asia_shanghai"],
                },
                "report_path": str(report_path.relative_to(REPO_ROOT)),
                "report_exists": True,
                "audit": {
                    "ok": audit.ok,
                    "row_count": audit.row_count,
                    "issues": [issue.__dict__ for issue in audit.issues],
                    "log": {
                        "ok": log_audit.ok,
                        "batch_search_calls": log_audit.batch_search_calls,
                        "planned_search_results": log_audit.planned_search_results,
                        "extract_calls": log_audit.extract_calls,
                        "quote_extract_calls": log_audit.quote_extract_calls,
                        "issues": [issue.__dict__ for issue in log_audit.issues],
                    },
                },
                "log_path": str(log_path.relative_to(REPO_ROOT)),
            }

    env = os.environ.copy()
    env["AGENTLOOM_WEB_SEARCH_NOW_UTC"] = now_utc

    code = (
        "from src.runner import run_app\n"
        f"print(run_app({WORKFLOW!r}, task_override={TASK!r}, log_to_file=True))\n"
    )
    started_at = datetime.now().astimezone().isoformat()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=REPO_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = 124
            log_file.write(f"\nBATCH_TIMEOUT after {timeout_seconds}s\n")
    finished_at = datetime.now().astimezone().isoformat()

    audit_payload = None
    if report_path.exists():
        audit = audit_report(report_path)
        log_audit = audit_log(log_path)
        audit_payload = {
            "ok": audit.ok and log_audit.ok,
            "row_count": audit.row_count,
            "issues": [issue.__dict__ for issue in audit.issues],
            "log": {
                "ok": log_audit.ok,
                "batch_search_calls": log_audit.batch_search_calls,
                "planned_search_results": log_audit.planned_search_results,
                "extract_calls": log_audit.extract_calls,
                "quote_extract_calls": log_audit.quote_extract_calls,
                "issues": [issue.__dict__ for issue in log_audit.issues],
            },
        }

    return {
        "now_utc": now_utc,
        "started_at": started_at,
        "finished_at": finished_at,
        "returncode": returncode,
        "timed_out": timed_out,
        "skipped": False,
        "context": {
            "us_trading_day": context["query_terms"]["us_trading_day_iso"],
            "a_share_prediction_date": context["query_terms"]["a_share_prediction_date_iso"],
            "news_window_start_asia_shanghai": context["news_window"]["start_asia_shanghai"],
            "news_window_end_asia_shanghai": context["news_window"]["end_asia_shanghai"],
        },
        "report_path": str(report_path.relative_to(REPO_ROOT)),
        "report_exists": report_path.exists(),
        "audit": audit_payload,
        "log_path": str(log_path.relative_to(REPO_ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="append", help="UTC ISO timestamp. Repeatable.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--skip-existing-ok", action="store_true")
    args = parser.parse_args()

    now_values = args.now or DEFAULT_NOWS_UTC
    if args.limit is not None:
        now_values = now_values[: args.limit]

    results = []
    for now_utc in now_values:
        result = _run_one(now_utc, args.timeout_seconds, args.skip_existing_ok)
        results.append(result)
        status = "PASS" if result["returncode"] == 0 and result.get("audit", {}).get("ok") else "FAIL"
        if result.get("skipped"):
            status = "SKIP"
        print(
            f"{status} {now_utc} -> {result['context']['us_trading_day']} "
            f"to {result['context']['a_share_prediction_date']} "
            f"report={result['report_path']} log={result['log_path']}",
            flush=True,
        )

    MANIFEST_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["returncode"] == 0 and r.get("audit", {}).get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
