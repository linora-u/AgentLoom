"""Reject the explicitly private Application in public commits."""

import subprocess


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    forbidden = [p for p in paths if p == "applications/news_agent" or p.startswith("applications/news_agent/")]
    if forbidden:
        raise SystemExit(f"Public repository contains {len(forbidden)} private news_agent files")
    print("Public repository contains no news_agent files")


if __name__ == "__main__":
    main()
