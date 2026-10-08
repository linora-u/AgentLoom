"""Publish a public-only tree, then merge the exact private PR head.

The coordinator executes from trusted private main. PR code is read as Git
objects, never imported or executed. The public repository gets its own commits.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import time


CONFIG_PATH = ".github/public-sync.json"
STATUS_CONTEXT = "Public sync"


class PublicDivergence(RuntimeError):
    """Public main does not match the private PR's base."""


def command(*args, cwd=None, data=None):
    result = subprocess.run(args, cwd=cwd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"{args[0]} failed")
    return result.stdout


def api(path, method="GET", payload=None):
    args = ["gh", "api", path, "--method", method]
    data = None
    if payload is not None:
        args += ["--input", "-"]
        data = json.dumps(payload).encode()
    raw = command(*args, data=data)
    return json.loads(raw) if raw else None


def local_tree(ref, cwd=None):
    tree = {}
    for entry in command("git", "ls-tree", "-rz", ref, cwd=cwd).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, kind, sha = metadata.decode().split()
            if kind != "blob":
                raise RuntimeError("Submodules need an explicit public export implementation")
            tree[path.decode()] = (mode, sha)
    return tree


def configured_prefixes(config):
    prefixes = config.get("private_prefixes")
    if not isinstance(prefixes, list):
        raise RuntimeError("private_prefixes must be a list of repository-relative paths")
    result = []
    for prefix in prefixes:
        if (not isinstance(prefix, str) or not prefix or prefix.startswith("/")
                or any(char in prefix for char in "\0\n\r")):
            raise RuntimeError("Invalid private path")
        parts = PurePosixPath(prefix).parts
        if not parts or ".." in parts or ".git" in parts or any(char in prefix for char in "*?[]"):
            raise RuntimeError("Private paths must be relative file/directory names without wildcards")
        result.append(PurePosixPath(prefix).as_posix().rstrip("/"))
    return tuple(result)


def load_config(path=CONFIG_PATH):
    config = json.loads(Path(path).read_text())
    configured_prefixes(config)
    return config


def prefixes_at_ref(ref):
    return configured_prefixes(json.loads(command("git", "show", f"{ref}:{CONFIG_PATH}")))


def is_private_path(path, prefixes):
    return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
               for prefix in prefixes)


def public_tree(tree, prefixes=None):
    if prefixes is None:
        prefixes = configured_prefixes(load_config())
    return {path: entry for path, entry in tree.items()
            if not is_private_path(path, prefixes)}


def tree_digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()


def validate_public_path(path, mode, prefixes=None):
    if prefixes is None:
        prefixes = configured_prefixes(load_config())
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or ".." in parts or ".git" in parts:
        raise RuntimeError("Unsafe public export path")
    if is_private_path(path, prefixes):
        raise RuntimeError("Configured private paths cannot be exported")
    if mode not in {"100644", "100755"}:
        raise RuntimeError("Public export refuses symbolic links and special files")


def materialize_public_tree(source_ref, desired, target, prefixes=None):
    """Replace a public worktree without copying private Git parents/history."""
    if prefixes is None:
        prefixes = configured_prefixes(load_config())
    current = local_tree("HEAD", target)
    removed = sorted(set(current) - set(desired))
    if removed:
        command("git", "update-index", "--force-remove", "-z", "--stdin", cwd=target,
                data="\0".join(removed).encode() + b"\0")
        for path in removed:
            destination = Path(target) / path
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
    changed = []
    for path, (mode, sha) in sorted(desired.items()):
        validate_public_path(path, mode, prefixes)
        if current.get(path) == (mode, sha):
            continue
        destination = Path(target) / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        destination.write_bytes(command("git", "show", f"{source_ref}:{path}"))
        destination.chmod(0o755 if mode == "100755" else 0o644)
        changed.append(path)
    if changed:
        command("git", "add", "-f", "--", *changed, cwd=target)
    staged = command("git", "write-tree", cwd=target).decode().strip()
    if local_tree(staged, target) != desired:
        raise RuntimeError("Staged public tree differs from the requested export")


class Coordinator:
    def __init__(self, config, *, wait_seconds=1200, interval=20):
        self.private = config["private_repository"]
        self.public = config["public_repository"]
        self.checks = config["required_public_checks"]
        self.wait_seconds = wait_seconds
        self.interval = interval

    def private_pr(self, number):
        return api(f"repos/{self.private}/pulls/{number}")

    def remote_tree(self, repo, ref):
        commit = api(f"repos/{repo}/commits/{ref}")
        tree = api(f"repos/{repo}/git/trees/{commit['commit']['tree']['sha']}?recursive=1")
        if tree.get("truncated"):
            raise RuntimeError("GitHub returned an incomplete tree")
        return {item["path"]: (item["mode"], item["sha"])
                for item in tree["tree"] if item["type"] != "tree"}

    def status(self, head, state, description, url=None):
        payload = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
        if url:
            payload["target_url"] = url
        api(f"repos/{self.private}/statuses/{head}", "POST", payload)

    def verify_private_head(self, number, head, base):
        current = self.private_pr(number)
        if current["state"] != "open" or current["draft"] or current["head"]["sha"] != head:
            raise RuntimeError("Private PR changed or is no longer ready; rerun for its current head")
        if current["base"]["sha"] != base:
            raise RuntimeError("Private main moved; update the PR and rerun before publishing")

    def private_main(self):
        return api(f"repos/{self.private}/git/ref/heads/main")["object"]["sha"]

    def verify_source(self, number, head, base):
        if number is not None:
            self.verify_private_head(number, head, base)
        elif self.private_main() != head:
            raise RuntimeError("Private main moved during recovery; retry for its current head")

    def merge_private(self, number, head):
        result = api(f"repos/{self.private}/pulls/{number}/merge", "PUT",
                     {"sha": head, "merge_method": "merge"})
        if not result.get("merged"):
            raise RuntimeError("Private merge did not complete")
        print(f"Private PR #{number} merged after public synchronization", flush=True)

    def ensure_public_pr(self, number, source_ref, base, desired, expected_public_base, prefixes):
        key = number if number is not None else "main-" + hashlib.sha256(source_ref.encode()).hexdigest()[:8]
        branch = f"codex/public-sync-{key}-{tree_digest(desired)[:12]}"
        owner = self.public.split("/", 1)[0]
        matches = api(f"repos/{self.public}/pulls?state=all&head={owner}:{branch}&per_page=100")
        existing = next((pr for pr in matches if pr["state"] == "open"), None)
        if existing:
            if self.remote_tree(self.public, existing["head"]["sha"]) != desired:
                raise RuntimeError("Existing sync branch was changed outside the coordinator")
            return existing
        if matches:
            raise RuntimeError("Matching public PR is closed; restore/reconcile public main before retrying")
        self.verify_source(number, source_ref, base)
        with tempfile.TemporaryDirectory(prefix="agentloom-public-sync-") as target:
            command("git", "clone", "--filter=blob:none", "--no-checkout", "--single-branch",
                    "--branch", "main", f"https://github.com/{self.public}.git", target)
            command("git", "sparse-checkout", "set", "--no-cone", "/*",
                    *(f"!/{prefix.rstrip('/')}" for prefix in prefixes), cwd=target)
            command("git", "checkout", "-b", branch, "origin/main", cwd=target)
            if local_tree("HEAD", target) != expected_public_base:
                raise RuntimeError("Public main moved while preparing the export; retry before publishing")
            materialize_public_tree(source_ref, desired, target, prefixes)
            command("git", "-c", "user.name=linora-u", "-c",
                    "user.email=260928258+linora-u@users.noreply.github.com",
                    "commit", "-m", "Sync public files", cwd=target)
            command("git", "push", f"git@github.com:{self.public}.git", f"HEAD:refs/heads/{branch}", cwd=target)
        return api(f"repos/{self.public}/pulls", "POST", {
            "title": "Sync public files", "head": branch, "base": "main",
            "body": (
                "## Summary\n\n```text\ndevelopment tree -> public files -> public CI -> public merge\n```\n\n"
                "Update public files without private Application files or private commit parents.\n\n"
                "## Evidence\n\n- **Before:** Public main differs from the requested export.\n"
                "  **After:** The staged public tree exactly matches the export; all four public checks must succeed before merge.\n\n"
                "## Merge Danger\n\n**Door:** two-way\n\n**Blast Radius:** public-source\n"
            )
        })

    def wait_public_checks(self, number, head, base, public_pr):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            self.verify_source(number, head, base)
            current = api(f"repos/{self.public}/pulls/{public_pr['number']}")
            if current["head"]["sha"] != public_pr["head"]["sha"] or current["state"] != "open":
                raise RuntimeError("Public PR changed or closed while waiting for CI")
            runs = api(f"repos/{self.public}/commits/{current['head']['sha']}/check-runs?per_page=100")["check_runs"]
            latest = {}
            for run in sorted(runs, key=lambda item: item["id"]):
                latest[run["name"]] = run
            failed = [name for name in self.checks if name in latest and
                      latest[name]["status"] == "completed" and latest[name]["conclusion"] != "success"]
            if failed:
                raise RuntimeError("Public CI failed: " + ", ".join(failed))
            if all(name in latest and latest[name]["status"] == "completed" and
                   latest[name]["conclusion"] == "success" for name in self.checks):
                if current.get("mergeable_state") == "behind":
                    api(f"repos/{self.public}/pulls/{current['number']}/update-branch", "PUT",
                        {"expected_head_sha": current["head"]["sha"]})
                    raise RuntimeError("Public main moved; public PR updated. Retry after the new CI run")
                return current
            if time.monotonic() >= deadline:
                raise RuntimeError("Public CI still pending; retry to resume the existing public PR")
            print(f"Waiting for public PR #{current['number']} CI", flush=True)
            time.sleep(self.interval)

    def process(self, pr):
        number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
        self.status(head, "pending", "Waiting for public synchronization")
        command("git", "fetch", "--filter=blob:none", "origin", base, head)
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base, head], capture_output=True)
        if ancestor.returncode:
            api(f"repos/{self.private}/pulls/{number}/update-branch", "PUT", {"expected_head_sha": head})
            raise RuntimeError("Private PR updated to current main; rerun for the new head")
        base_prefixes, head_prefixes = prefixes_at_ref(base), prefixes_at_ref(head)
        original = public_tree(local_tree(base), base_prefixes)
        desired = public_tree(local_tree(head), head_prefixes)
        if original == desired:
            self.verify_private_head(number, head, base)
            self.status(head, "success", "Private-only change; no public PR or tests required")
            self.merge_private(number, head)
            return
        actual = self.remote_tree(self.public, "main")
        if actual != desired:
            if actual != original:
                raise PublicDivergence("Public main diverged; bring public changes into private main before exporting")
            public_pr = self.ensure_public_pr(number, head, base, desired, actual, head_prefixes)
            self.status(head, "pending", "Waiting for public PR to merge", public_pr["html_url"])
            public_pr = self.wait_public_checks(number, head, base, public_pr)
            self.verify_private_head(number, head, base)
            if self.remote_tree(self.public, public_pr["head"]["sha"]) != desired:
                raise RuntimeError("Tested public tree no longer matches the requested export")
            result = api(f"repos/{self.public}/pulls/{public_pr['number']}/merge", "PUT",
                         {"sha": public_pr["head"]["sha"], "merge_method": "squash"})
            if not result.get("merged") or self.remote_tree(self.public, result["sha"]) != desired:
                raise RuntimeError("Public merge did not produce the expected public tree")
            print(f"Public PR #{public_pr['number']} merged", flush=True)
        if self.remote_tree(self.public, "main") != desired:
            raise RuntimeError("Public main moved after publication; reconcile before merging private PR")
        self.verify_private_head(number, head, base)
        self.status(head, "success", "Matching public files are merged")
        self.merge_private(number, head)

    def find_public_base(self, head, actual):
        """Find a published first-parent snapshot; refuse unrelated public edits."""
        ancestors = command("git", "rev-list", "--first-parent", head).decode().splitlines()[1:]
        for base in ancestors:
            if not command("git", "ls-tree", "--name-only", base, "--", CONFIG_PATH).strip():
                break
            if public_tree(local_tree(base), prefixes_at_ref(base)) == actual:
                return base
        raise PublicDivergence("Public main has independent changes; reconcile them in private main before recovery")

    def reconcile_main(self):
        """Recover public changes already merged/pushed to unprotected private main."""
        head = self.private_main()
        try:
            command("git", "fetch", "--filter=blob:none", "origin", head)
            prefixes = prefixes_at_ref(head)
            desired = public_tree(local_tree(head), prefixes)
            actual = self.remote_tree(self.public, "main")
            if actual == desired:
                print("Private main's public files are already synchronized", flush=True)
                return
            self.status(head, "pending", "Recovering public files from private main")
            base = self.find_public_base(head, actual)
            public_pr = self.ensure_public_pr(None, head, base, desired, actual, prefixes)
            self.status(head, "pending", "Waiting for public recovery PR", public_pr["html_url"])
            public_pr = self.wait_public_checks(None, head, base, public_pr)
            self.verify_source(None, head, base)
            if self.remote_tree(self.public, public_pr["head"]["sha"]) != desired:
                raise RuntimeError("Tested public tree no longer matches private main")
            result = api(f"repos/{self.public}/pulls/{public_pr['number']}/merge", "PUT",
                         {"sha": public_pr["head"]["sha"], "merge_method": "squash"})
            if not result.get("merged") or self.remote_tree(self.public, result["sha"]) != desired:
                raise RuntimeError("Public recovery merge did not produce the expected tree")
            self.verify_source(None, head, base)
            if self.remote_tree(self.public, "main") != desired:
                raise RuntimeError("Public main moved after recovery; reconcile before retrying")
            self.status(head, "success", "Private main's public files are synchronized")
            print(f"Public recovery PR #{public_pr['number']} merged", flush=True)
        except Exception as exc:
            self.status(head, "failure", str(exc))
            raise


def main():
    config = load_config()
    if os.environ.get("GITHUB_REPOSITORY", config["private_repository"]) != config["private_repository"]:
        raise SystemExit("Coordinator only runs in the private repository")
    coordinator = Coordinator(config)
    requested = os.environ.get("SYNC_PR_NUMBER", "").strip()
    prs = []
    if requested:
        prs = [coordinator.private_pr(int(requested))]
    else:
        page = 1
        while True:
            batch = api(f"repos/{coordinator.private}/pulls?state=open&base=main&sort=created&direction=asc&per_page=100&page={page}")
            prs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    for pr in prs:
        if pr["state"] != "open" or pr["draft"] or pr["head"]["repo"]["full_name"] != coordinator.private:
            continue
        # Refresh the base after any preceding PR merged in this same run.
        pr = coordinator.private_pr(pr["number"])
        try:
            try:
                coordinator.process(pr)
            except PublicDivergence:
                # Manual main updates can put public behind a later PR's base.
                # Already-published PRs are resumed before this recovery path.
                coordinator.reconcile_main()
                coordinator.process(coordinator.private_pr(pr["number"]))
        except Exception as exc:
            coordinator.status(pr["head"]["sha"], "failure", str(exc))
            raise
    coordinator.reconcile_main()


if __name__ == "__main__":
    main()
