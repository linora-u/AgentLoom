"""Reject configured private paths in public commits and their history."""

import argparse
import subprocess

from public_sync import configured_prefixes, is_private_path, load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--config", default=".github/public-sync.json")
    args = parser.parse_args()
    prefixes = configured_prefixes(load_config(args.config))
    history = subprocess.check_output([
        "git", "rev-list", "-1", args.ref, "--", *prefixes
    ]).strip() if prefixes else b""
    if history:
        raise SystemExit("Public commit history contains private files from configured paths")
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "-z", args.ref]).decode().split("\0")
    forbidden = [p for p in paths if is_private_path(p, prefixes)]
    if forbidden:
        raise SystemExit(f"Public repository contains {len(forbidden)} configured private files")
    print("Public repository and commit history contain no configured private files")


if __name__ == "__main__":
    main()
