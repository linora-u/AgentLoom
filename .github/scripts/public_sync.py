"""Publish filtered commit history, then merge the exact private PR head.

The coordinator executes from trusted private main. PR code is read as Git
objects, never imported or executed. The public repository gets its own commits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

CONFIG_PATH = ".github/public-sync.json"
STATUS_CONTEXT = "Public sync"
STATE_BRANCH = "codex/public-sync-state"
STATE_PATH = "state.json"
HISTORY_MARKER = "<!-- agentloom-public-history\n"


class PublicDivergence(RuntimeError):
    """Public main does not match the private PR's base."""


def command(*args, cwd=None, data=None, env=None):
    result = subprocess.run(args, cwd=cwd, input=data, env=env, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"{args[0]} failed")
    return result.stdout


def api(path, method="GET", payload=None, *, missing_ok=False):
    args = ["gh", "api", path, "--method", method]
    data = None
    if payload is not None:
        args += ["--input", "-"]
        data = json.dumps(payload).encode()
    try:
        raw = command(*args, data=data)
    except RuntimeError as exc:
        if missing_ok and "HTTP 404" in str(exc):
            return None
        raise
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
    """Stage only approved blobs; never import private commits or worktrees."""
    if prefixes is None:
        prefixes = configured_prefixes(load_config())
    current = local_tree("HEAD", target)
    command("git", "read-tree", "HEAD", cwd=target)
    removed = sorted(set(current) - set(desired))
    if removed:
        command("git", "update-index", "--force-remove", "-z", "--stdin", cwd=target,
                data="\0".join(removed).encode() + b"\0")
    changed = []
    for path, (mode, sha) in sorted(desired.items()):
        validate_public_path(path, mode, prefixes)
        if current.get(path) == (mode, sha):
            continue
        blob = command("git", "hash-object", "-w", "--stdin", cwd=target,
                       data=command("git", "show", f"{source_ref}:{path}")).decode().strip()
        if blob != sha:
            raise RuntimeError("Exported blob differs from the source")
        changed.append(f"{mode} {sha}\t{path}".encode() + b"\0")
    if changed:
        command("git", "update-index", "-z", "--index-info", cwd=target, data=b"".join(changed))
    staged = command("git", "write-tree", cwd=target).decode().strip()
    if local_tree(staged, target) != desired:
        raise RuntimeError("Staged public tree differs from the requested export")
    return staged


def is_ancestor(ancestor, head, cwd=None):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, head],
                            cwd=cwd, capture_output=True)
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.returncode == 0


def public_history(base, head, prefixes):
    """Plan parents before children, hiding paths made private by later commits."""
    rows = command("git", "rev-list", "--reverse", "--topo-order", "--parents",
                   f"{base}..{head}").decode().splitlines()
    history = [{"sha": fields[0], "parents": fields[1:]} for fields in map(str.split, rows)]
    protected = {entry["sha"]: set(prefixes) | set(prefixes_at_ref(entry["sha"]))
                 for entry in history}
    for entry in reversed(history):
        for parent in entry["parents"]:
            if parent in protected:
                protected[parent].update(protected[entry["sha"]])
    for entry in history:
        entry["prefixes"] = tuple(sorted(protected[entry["sha"]]))
        entry["tree"] = public_tree(local_tree(entry["sha"]), entry["prefixes"])
    return history


def has_public_changes(history):
    """Ignore private-only branches rebased by merging an already published base."""
    collapsed = {}
    for entry in history:
        parents = list(dict.fromkeys(collapsed.get(parent, parent) for parent in entry["parents"]))
        parents = [parent for parent in parents if not any(
            parent != other and is_ancestor(parent, other) for other in parents
        )]
        trees = [public_tree(local_tree(parent), entry["prefixes"]) for parent in parents]
        if not trees or any(tree != entry["tree"] for tree in trees):
            return True
        # Distinct private parents may represent the same public snapshot.
        collapsed[entry["sha"]] = parents[0]
    return False


def source_commit(ref):
    """Read the original message bytes and identity without running PR code."""
    headers, message = command("git", "cat-file", "commit", ref).split(b"\n\n", 1)
    fields = {}
    for line in headers.splitlines():
        if line.startswith((b"author ", b"committer ", b"encoding ")):
            key, value = line.split(b" ", 1)
            fields[key] = value
    name, rest = fields[b"author"].rsplit(b" <", 1)
    email, author_date = rest.rsplit(b"> ", 1)
    committer_date = fields[b"committer"].rsplit(b"> ", 1)[1]
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": os.fsdecode(name), "GIT_AUTHOR_EMAIL": os.fsdecode(email),
        "GIT_AUTHOR_DATE": author_date.decode(),
        "GIT_COMMITTER_NAME": "linora-u",
        "GIT_COMMITTER_EMAIL": "260928258+linora-u@users.noreply.github.com",
        # A retry against the same public parent must reproduce identical objects.
        "GIT_COMMITTER_DATE": committer_date.decode(),
    })
    return message, env, fields.get(b"encoding", b"UTF-8").decode()


def export_public_history(history, target, prefixes):
    """Rebuild the filtered DAG on public parents, collapsing private-only nodes."""
    public_base = command("git", "rev-parse", "HEAD", cwd=target).decode().strip()
    if prefixes and command("git", "rev-list", "-1", public_base, "--", *prefixes, cwd=target).strip():
        raise RuntimeError("Public history contains a configured private path; clean its published history first")
    public_ancestors = command("git", "rev-list", public_base, cwd=target).decode().splitlines()
    boundary_cache = {}

    def public_parent(source_parent, protected):
        key = (source_parent, protected)
        if key not in boundary_cache:
            tree = public_tree(local_tree(source_parent), protected)
            match = next((ref for ref in public_ancestors if local_tree(ref, target) == tree), None)
            if match is None:
                raise RuntimeError("Source parent has no matching public snapshot; reconcile history before exporting")
            boundary_cache[key] = match
        return boundary_cache[key]

    mapped, commits, exported = {}, [], []
    for entry in history:
        parents = list(dict.fromkeys(
            mapped[parent] if parent in mapped else public_parent(parent, entry["prefixes"])
            for parent in entry["parents"]
        ))
        # Ordinary private PR merge wrappers become redundant after filtering.
        parents = [parent for parent in parents if not any(
            parent != other and is_ancestor(parent, other, target) for other in parents
        )]
        if not parents:
            parents = [public_base]
        if len(parents) == 1 and local_tree(parents[0], target) == entry["tree"]:
            public_sha = parents[0]
            created = False
        else:
            tree = materialize_public_tree(entry["sha"], entry["tree"], target, entry["prefixes"])
            message, env, encoding = source_commit(entry["sha"])
            args = ["git", "-c", "commit.gpgsign=false", "-c", f"i18n.commitEncoding={encoding}",
                    "commit-tree", tree]
            for parent in parents:
                args += ["-p", parent]
            public_sha = command(*args, cwd=target, data=message, env=env).decode().strip()
            command("git", "update-ref", "HEAD", public_sha, cwd=target)
            exported.append(entry["sha"])
            created = True
        mapped[entry["sha"]] = public_sha
        commits.append({"source": entry["sha"], "public": public_sha, "exported": created})
    if not history or not exported:
        raise RuntimeError("No public commit changes to publish")
    command("git", "reset", "--hard", mapped[history[-1]["sha"]], cwd=target)
    if prefixes and command("git", "rev-list", "-1", "HEAD", "--", *prefixes, cwd=target).strip():
        raise RuntimeError("Public history contains a configured private path; clean its published history first")
    message, _, _ = source_commit(exported[-1])
    title = (message.decode(errors="replace").splitlines() or ["Public changes"])[0]
    return commits, title


def publication_metadata(body):
    if HISTORY_MARKER not in (body or ""):
        raise RuntimeError("Sync PR is missing its source commit mapping")
    metadata = json.loads(body.split(HISTORY_MARKER, 1)[1].split("\n-->", 1)[0])
    if (metadata.get("version") != 1
            or not all(re.fullmatch(r"[0-9a-f]{40}", metadata.get(key, ""))
                       for key in ("source_base", "source_head"))
            or not valid_commit_mapping(metadata.get("commits"))):
        raise RuntimeError("Invalid sync PR source commit mapping")
    return metadata


def valid_commit_mapping(commits):
    return isinstance(commits, list) and all(
        isinstance(entry, dict) and isinstance(entry.get("exported"), bool)
        and all(isinstance(entry.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", entry[key])
                for key in ("source", "public")) for entry in commits
    )


class Coordinator:
    def __init__(self, config, *, wait_seconds=1200, interval=20):
        self.private = config["private_repository"]
        self.public = config["public_repository"]
        self.checks = config["required_public_checks"]
        self.wait_seconds = wait_seconds
        self.interval = interval
        self._state_loaded = False
        self._state = None
        self._state_file_sha = None
        self._private_pr_title = None

    def load_state(self):
        """The durable cursor and mappings stay on a metadata-only private branch."""
        if not self._state_loaded:
            file = api(f"repos/{self.private}/contents/{STATE_PATH}?ref={STATE_BRANCH}", missing_ok=True)
            if file:
                state = json.loads(base64.b64decode(file["content"]))
                if (state.get("version") != 1
                        or not all(re.fullmatch(r"[0-9a-f]{40}", state.get(key, ""))
                                   for key in ("source_head", "public_head"))
                        or not valid_commit_mapping(state.get("commits"))):
                    raise RuntimeError("Invalid private synchronization state")
                self._state, self._state_file_sha = state, file["sha"]
            self._state_loaded = True
        return self._state

    def save_publication(self, head, public_head, metadata=None):
        self.load_state()
        state = {"version": 1, "source_head": head, "public_head": public_head,
                 "commits": metadata["commits"] if metadata else []}
        if self._state == state:
            return
        content = json.dumps(state, indent=2) + "\n"
        if self._state_file_sha:
            result = api(f"repos/{self.private}/contents/{STATE_PATH}", "PUT", {
                "branch": STATE_BRANCH, "sha": self._state_file_sha,
                "message": "Record published public commit history",
                "content": base64.b64encode(content.encode()).decode(),
            })
            self._state_file_sha = result["content"]["sha"]
        else:
            tree = api(f"repos/{self.private}/git/trees", "POST", {
                "tree": [{"path": STATE_PATH, "mode": "100644", "type": "blob", "content": content}]
            })
            commit = api(f"repos/{self.private}/git/commits", "POST", {
                "message": "Initialize public synchronization state", "tree": tree["sha"], "parents": []
            })
            api(f"repos/{self.private}/git/refs", "POST", {
                "ref": f"refs/heads/{STATE_BRANCH}", "sha": commit["sha"]
            })
            # Read the blob SHA before a subsequent optimistic update in this run.
            self._state_loaded = False
            self.load_state()
        self._state = state

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

    def public_main(self):
        return api(f"repos/{self.public}/git/ref/heads/main")["object"]["sha"]

    def recover_publication(self, head, actual):
        """Recover a cursor lost after public merge but before the private state write."""
        ancestors = set(command("git", "rev-list", head).decode().splitlines())
        page = 1
        while True:
            prs = api(f"repos/{self.public}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100&page={page}")
            for pr in sorted(prs, key=lambda item: item.get("merged_at") or "", reverse=True):
                if not pr.get("merged_at") or not pr["head"]["ref"].startswith("codex/public-sync-history-"):
                    continue
                metadata = publication_metadata(pr["body"])
                source = metadata["source_head"]
                if source not in ancestors:
                    continue
                if (public_tree(local_tree(source), prefixes_at_ref(source)) == actual
                        and self.remote_tree(self.public, pr["merge_commit_sha"]) == actual):
                    self.verify_source(None, head, None)
                    self.save_publication(source, pr["merge_commit_sha"], metadata)
                    return
            if len(prs) < 100:
                return
            page += 1

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

    def public_branch(self, number, source_ref, base):
        key = number if number is not None else "main"
        # Include source history, so equal final trees with different commits cannot collide.
        digest = hashlib.sha256(f"{base}\0{source_ref}".encode()).hexdigest()[:16]
        return f"codex/public-sync-history-{key}-{digest}"

    def matching_public_pr(self, number, source_ref, base, desired):
        branch = self.public_branch(number, source_ref, base)
        owner = self.public.split("/", 1)[0]
        matches = api(f"repos/{self.public}/pulls?state=all&head={owner}:{branch}&per_page=100")
        existing = next((pr for pr in matches if pr["state"] == "open" or pr.get("merged_at")), None)
        if existing:
            metadata = publication_metadata(existing["body"])
            if metadata["source_head"] != source_ref or metadata["source_base"] != base:
                raise RuntimeError("Existing sync PR has a different source history")
            if self.remote_tree(self.public, existing["head"]["sha"]) != desired:
                raise RuntimeError("Existing sync branch was changed outside the coordinator")
            return existing
        if matches:
            raise RuntimeError("Matching public PR is closed; restore/reconcile public main before retrying")
        return None

    def ensure_public_pr(self, number, source_ref, base, desired, expected_public_base, prefixes):
        existing = self.matching_public_pr(number, source_ref, base, desired)
        if existing:
            return existing
        branch = self.public_branch(number, source_ref, base)
        self.verify_source(number, source_ref, base)
        history = public_history(base, source_ref, prefixes)
        with tempfile.TemporaryDirectory(prefix="agentloom-public-sync-") as target:
            command("git", "clone", "--filter=blob:none", "--no-checkout", "--single-branch",
                    "--branch", "main", f"https://github.com/{self.public}.git", target)
            command("git", "sparse-checkout", "set", "--no-cone", "/*",
                    *(f"!/{prefix.rstrip('/')}" for prefix in prefixes), cwd=target)
            command("git", "branch", branch, "origin/main", cwd=target)
            command("git", "symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=target)
            if local_tree("HEAD", target) != expected_public_base:
                raise RuntimeError("Public main moved while preparing the export; retry before publishing")
            pending = command("git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}", cwd=target).split()
            if pending:
                # A push can succeed before PR creation fails. Rebuild on its original
                # public base even if public main subsequently gains an empty commit.
                command("git", "fetch", "--filter=blob:none", "origin", f"refs/heads/{branch}", cwd=target)
                anchor = command("git", "merge-base", "origin/main", "FETCH_HEAD", cwd=target).decode().strip()
                command("git", "update-ref", "HEAD", anchor, cwd=target)
            commits, title = export_public_history(history, target, prefixes)
            if local_tree("HEAD", target) != desired:
                raise RuntimeError("Exported history does not end at the requested public tree")
            if pending and command("git", "rev-parse", "HEAD", cwd=target).strip() != pending[0]:
                raise RuntimeError("Unattached sync branch was changed outside the coordinator")
            self.verify_source(number, source_ref, base)
            command("git", "push", f"git@github.com:{self.public}.git", f"HEAD:refs/heads/{branch}", cwd=target)
        metadata = {"version": 1, "source_base": base, "source_head": source_ref, "commits": commits}
        return api(f"repos/{self.public}/pulls", "POST", {
            "title": (self._private_pr_title or title)[:256], "head": branch, "base": "main",
            "body": (
                "## Summary\n\n```text\ndevelopment tree -> public files -> public CI -> public merge\n```\n\n"
                "Preserve original commit messages, authors, and author dates for public changes. "
                "Private-only commits are skipped; private files and Git parents are never copied.\n\n"
                "## Evidence\n\n- **Before:** Source commits have not been published with their individual history.\n"
                "  **After:** The staged public tree exactly matches the export; all four public checks must succeed before merge.\n\n"
                "## Merge Danger\n\n**Door:** two-way\n\n**Blast Radius:** public-source\n\n"
                + HISTORY_MARKER + json.dumps(metadata) + "\n-->\n"
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
        self._private_pr_title = pr.get("title")
        self.status(head, "pending", "Waiting for public synchronization")
        command("git", "fetch", "--filter=blob:none", "origin", base, head)
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base, head], capture_output=True)
        if ancestor.returncode:
            api(f"repos/{self.private}/pulls/{number}/update-branch", "PUT", {"expected_head_sha": head})
            raise RuntimeError("Private PR updated to current main; rerun for the new head")
        base_prefixes, head_prefixes = prefixes_at_ref(base), prefixes_at_ref(head)
        original = public_tree(local_tree(base), base_prefixes)
        desired = public_tree(local_tree(head), head_prefixes)
        history = public_history(base, head, head_prefixes)
        if not has_public_changes(history):
            self.verify_private_head(number, head, base)
            self.status(head, "success", "Private-only change; no public PR or tests required")
            self.merge_private(number, head)
            return
        actual = self.remote_tree(self.public, "main")
        public_pr = self.matching_public_pr(number, head, base, desired)
        metadata = publication_metadata(public_pr["body"]) if public_pr else None
        if public_pr and public_pr.get("merged_at"):
            if actual != desired:
                raise PublicDivergence("Public main moved after publication; reconcile before merging private PR")
            public_head = public_pr["merge_commit_sha"]
        elif actual == desired and original != desired and public_pr is None:
            # Bootstrap the cursor for a snapshot published by the previous coordinator.
            public_head = self.public_main()
        else:
            if actual != original:
                raise PublicDivergence("Public main diverged; bring public changes into private main before exporting")
            public_pr = public_pr or self.ensure_public_pr(number, head, base, desired, actual, head_prefixes)
            metadata = publication_metadata(public_pr["body"])
            self.status(head, "pending", "Waiting for public PR to merge", public_pr["html_url"])
            public_pr = self.wait_public_checks(number, head, base, public_pr)
            self.verify_private_head(number, head, base)
            if self.remote_tree(self.public, public_pr["head"]["sha"]) != desired:
                raise RuntimeError("Tested public tree no longer matches the requested export")
            result = api(f"repos/{self.public}/pulls/{public_pr['number']}/merge", "PUT",
                         {"sha": public_pr["head"]["sha"], "merge_method": "merge"})
            if not result.get("merged") or self.remote_tree(self.public, result["sha"]) != desired:
                raise RuntimeError("Public merge did not produce the expected public tree")
            print(f"Public PR #{public_pr['number']} merged", flush=True)
            public_head = result["sha"]
        if self.remote_tree(self.public, "main") != desired:
            raise RuntimeError("Public main moved after publication; reconcile before merging private PR")
        self.verify_private_head(number, head, base)
        self.save_publication(head, public_head, metadata)
        self.status(head, "success", "Matching public files are merged")
        self.merge_private(number, head)

    def find_public_base(self, head, actual):
        """Find a published first-parent snapshot; refuse unrelated public edits."""
        state = self.load_state()
        if state and is_ancestor(state["source_head"], head):
            source = state["source_head"]
            if (public_tree(local_tree(source), prefixes_at_ref(source)) == actual
                    and self.remote_tree(self.public, state["public_head"]) == actual):
                return source
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
        self._private_pr_title = None
        try:
            command("git", "fetch", "--filter=blob:none", "origin", head)
            prefixes = prefixes_at_ref(head)
            desired = public_tree(local_tree(head), prefixes)
            actual = self.remote_tree(self.public, "main")
            state = self.load_state()
            if actual == desired:
                if state is None or not is_ancestor(state["source_head"], head) or public_tree(
                    local_tree(state["source_head"]), prefixes_at_ref(state["source_head"])
                ) != actual or state["public_head"] != self.public_main():
                    self.recover_publication(head, actual)
                if self.load_state() is None:
                    self.verify_source(None, head, None)
                    self.save_publication(head, self.public_main())
                    print("Private main's public files are already synchronized", flush=True)
                    return
            self.status(head, "pending", "Recovering public files from private main")
            base = self.find_public_base(head, actual)
            history = public_history(base, head, prefixes)
            if not has_public_changes(history):
                self.verify_source(None, head, base)
                self.status(head, "success", "Private main has no unpublished public commits")
                print("Private main has no unpublished public commits", flush=True)
                return
            public_pr = self.ensure_public_pr(None, head, base, desired, actual, prefixes)
            self.status(head, "pending", "Waiting for public recovery PR", public_pr["html_url"])
            metadata = publication_metadata(public_pr["body"])
            if public_pr.get("merged_at"):
                public_head = public_pr["merge_commit_sha"]
            else:
                public_pr = self.wait_public_checks(None, head, base, public_pr)
                self.verify_source(None, head, base)
                if self.remote_tree(self.public, public_pr["head"]["sha"]) != desired:
                    raise RuntimeError("Tested public tree no longer matches private main")
                result = api(f"repos/{self.public}/pulls/{public_pr['number']}/merge", "PUT",
                             {"sha": public_pr["head"]["sha"], "merge_method": "merge"})
                if not result.get("merged") or self.remote_tree(self.public, result["sha"]) != desired:
                    raise RuntimeError("Public recovery merge did not produce the expected tree")
                public_head = result["sha"]
            self.verify_source(None, head, base)
            if self.remote_tree(self.public, "main") != desired:
                raise RuntimeError("Public main moved after recovery; reconcile before retrying")
            self.save_publication(head, public_head, metadata)
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
