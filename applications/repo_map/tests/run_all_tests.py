"""
repo_map 测试统一运行入口。

分两组：
  Group A（无 LLM，快速）：test_scan_rank_tool.py
    - 测试增量扫描逻辑（git hash + mtime）
    - 不需要 LLM，运行 ~1s

  Group B（有 LLM，较慢）：run_demo.py
    - 直接调用完整链路：scan → markdown → run_analysis_loop (LLM)
    - 验证子 Agent 创建、参数传递、analysis.md 生成、日志输出
    - 需要真实 LLM，运行 ~2-5 分钟

用法：
  # 只运行无 LLM 测试（快速验证增量逻辑）
  python applications/repo_map/tests/run_all_tests.py

  # 运行所有测试（含 LLM）
  python applications/repo_map/tests/run_all_tests.py --all

  # 只运行有 LLM 测试
  python applications/repo_map/tests/run_all_tests.py --llm-only
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TESTS_DIR = Path(__file__).parent


def run_pytest(args: list, label: str) -> int:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    cmd = [sys.executable, "-m", "pytest"] + args + ["-v", "--tb=short"]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return result.returncode


def run_script(script_path: str, label: str) -> int:
    """直接运行 Python 脚本（非 pytest），用于 LLM 集成验证。"""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    cmd = [sys.executable, script_path]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Run repo_map tests")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Run all tests including LLM tests")
    group.add_argument("--llm-only", action="store_true", help="Run only LLM tests")
    args = parser.parse_args()

    exit_codes = []

    if not args.llm_only:
        # Group A: 无 LLM 测试（增量扫描逻辑）
        code = run_pytest(
            [str(TESTS_DIR / "test_scan_rank_tool.py")],
            "Group A: scan_rank_tool tests (no LLM, ~1s)",
        )
        exit_codes.append(code)

    if args.all or args.llm_only:
        # Group B: 有 LLM 集成烟测
        code = run_script(
            str(TESTS_DIR / "run_demo.py"),
            "Group B: repo_map_app fixture smoke test (with LLM)",
        )
        exit_codes.append(code)


    print(f"\n{'='*60}")
    if all(c == 0 for c in exit_codes):
        print("  ALL TESTS PASSED")
    else:
        print(f"  SOME TESTS FAILED (exit codes: {exit_codes})")
    print(f"{'='*60}\n")

    sys.exit(max(exit_codes) if exit_codes else 0)


if __name__ == "__main__":
    main()
