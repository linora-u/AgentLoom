"""Application/Pi adapters to the vendored Harness² method, without method decisions."""
from __future__ import annotations

import json
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml

from .models import ExecutionReceipt, fingerprint, json_value
from .snapshot import derive_candidate

METHOD_REVISION = "harness2-1cfe254-agentloom-v2"


def method_identity():
    import hashlib
    import harness2.core
    sources = {}
    for root, prefix in ((Path(harness2.core.__file__).parent, "upstream"), (Path(__file__).parent, "adapter")):
        for path in sorted(root.glob("*.py")):
            sources[prefix + "/" + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"revision": METHOD_REVISION, "content_fingerprint": fingerprint(sources)}


def evaluator_identity(reference):
    import hashlib
    import inspect
    if not reference:
        return {}
    from .controller import load_driver
    evaluator = load_driver(reference)
    target = evaluator if inspect.isfunction(evaluator) else type(evaluator)
    source = inspect.getsourcefile(target)
    if not source or not Path(source).is_file():
        raise ValueError("Trusted development evaluator requires an inspectable source identity")
    return {"reference": reference, "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest()}


def _read(path):
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def artifacts_intact(receipt):
    import hashlib
    for artifact in receipt.artifacts:
        if "path" in artifact:
            path = Path(artifact["path"])
            if not path.is_file() or path.is_symlink() or "sha256" not in artifact:
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"].removeprefix("sha256:"):
                return False
    return True


def resource_tree(controller):
    """Only executable configuration and its declared resource closure is evidence.

    The immutable dependency bundle remains complete; historical reviews, business
    Python and input databases do not become editable harness files by proximity.
    """
    from .snapshot import _references, _worker_resources
    bundle = controller.base
    root, app = bundle.project_root, bundle.application_root
    role_paths = {(app / reference).resolve() for _, reference in controller.policy.roles.overrides()}
    selected = set()
    for folder in (app / "workflows", app / "prompts", app / "prompt", app / "agent", app / "skills", root / "skills"):
        if folder.is_dir():
            selected.update(path for path in folder.rglob("*") if path.is_file() and path.resolve() not in role_paths)
    for name in bundle.manifest.get("editable_resources", {}):
        selected.add(root / name)
    # Explicit YAML references capture shared Chinese prompts, Skill resources
    # and tool configuration without indiscriminately exposing every app file.
    pending = list(selected)
    scanned = set()
    while pending:
        path = pending.pop()
        if path in scanned or path.suffix not in {".yaml", ".yml", ".json"}:
            continue
        scanned.add(path)
        value = json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())
        owner = app if path.is_relative_to(app) else root
        for target in _worker_resources(value, path, root):
            if target not in selected and target.resolve() not in role_paths:
                selected.add(target)
                pending.append(target)
        for reference in _references(value):
            target = (owner / reference).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise ValueError("Harness resource reference escapes its frozen dependency closure")
            items = {item for item in target.rglob("*") if item.is_file()} if target.is_dir() else {target}
            items.difference_update(role_paths)
            pending.extend(items - selected)
            selected.update(items)
    return {path.relative_to(root).as_posix(): _read(path) for path in sorted(selected)
            if path.suffix != ".py" or "skills" in path.parts}


def _surface(controller, resources):
    from harness2.core.tree import SurfaceSpec
    app = controller.base.application
    return SurfaceSpec(
        name="agentloom-application", always_on=(), always_on_max=1_000_000,
        always_on_cap_reason="Application prompts retain the owning runtime's constraints",
        skill_dir="skills", skill_name_rule="the Application's configured Skill name",
        trigger_hint="declare the Skill in its owning Agent YAML",
        autotrigger_file="", trigger_line="", subagent_requires_mode=False,
        plugin_dir=None, hooks_file=None, application=True,
        resource_paths=tuple(resources),
        editable_globs=(f"{app}/workflows/**/*.yaml", f"{app}/workflows/*.yaml",
                        f"{app}/skills/**", "skills/**", f"{app}/prompts/**", f"{app}/prompt/**"),
        surface_md=("The editable unit is the complete AgentLoom Application. Files retain their project-relative paths. "
                    "Change only the configured components: " + ", ".join(controller.policy.editable) + ". "
                    "YAML fixed fields (task, acceptance, runtime, models, hooks) are frozen. Agent definitions and delegation "
                    "may change within these constraints. Preserve supporting Skill resources and declared dependencies. "
                    "Do not execute application code in this improvement session. Historical Library entries are previous tasks, "
                    "not current execution evidence. All edits are validated by the trusted Application driver."),
    )


def _write_resources(tree, root):
    root.mkdir(parents=True, exist_ok=True)
    for name, body in tree.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))


def _role_of(label):
    token = label.lower()
    if "reflect" in token or "distil" in token or token.startswith("experience."):
        return "reflect"
    if "select" in token:
        return "select"
    if token.startswith("inv"):
        return "general"
    if token.startswith("dom"):
        return "domain"
    if token.startswith(("agg", "c")):
        return "compose"
    raise ValueError("Unknown upstream session label: " + label)


class PiTransport:
    """One host execution bridge; cache, retry and harvesting remain upstream."""
    def __init__(self, controller):
        self.controller = controller

    def __call__(self, cwd, mission, system, timeout, *, label):
        controller = self.controller
        controller._check_stop()
        role = _role_of(label)
        import hashlib
        evidence = {path.relative_to(cwd / "context").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((cwd / "context").rglob("*")) if path.is_file()}
        base_key = "pi." + re.sub(r"[^A-Za-z0-9_.-]", "_", label) + "." + fingerprint([label, mission, system, evidence])[:16]
        attempt = 1
        while True:
            key = base_key + ".t" + str(attempt)
            context = controller._context(key, controller.base, role=True)
            previous = context.root / "process/result.json"
            if not previous.exists() or json.loads(previous.read_text()).get("result", {}).get("status") == "completed":
                break
            attempt += 1
        workspace = context.workspace_root
        workspace.mkdir(parents=True, exist_ok=True)
        # Managed operation identity survives a transport/controller crash; the
        # upstream temporary directory is never part of that identity.
        if not (context.root / "process/input.json").exists():
            if (cwd / "context").is_dir():
                shutil.copytree(cwd / "context", workspace / "context", dirs_exist_ok=True)
            (workspace / "MISSION.md").write_text((system + "\n\n" if system else "") + mission)
        payload = {"role": role, "stage": key, "definition": getattr(controller.policy.roles, role),
                   "mission": mission, "system": system}
        controller._emit("role.started", stage=key, role=role)
        result = controller._process(controller.base, context, "agentloom.app.harnessing.roles:execute_role", payload)
        receipt = result.get("receipt", {})
        controller._usage(receipt.get("usage_observations", ()))
        record = {"stage": key, "role": role, "label": label, "status": result.get("status"),
                  "output": result.get("output", ""), "receipt": receipt}
        with controller._mutex:
            controller.store.write(f"roles/{key}/receipt.json", record)
            controller.role_receipts.append(record)
        if result.get("status") != "completed":
            return "", 0.0, str(result.get("error", "Pi role failed"))
        # Upstream will collect exactly context/harness from its own workspace.
        if (workspace / "context").is_dir():
            shutil.rmtree(cwd / "context", ignore_errors=True)
            shutil.copytree(workspace / "context", cwd / "context")
        controller._emit("role.completed", stage=key, role=role)
        return result["output"], 0.0, ""


class ApplicationAdapter:
    """The complete Application execution seam expected by Harness²."""
    def __init__(self, controller):
        from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec
        self.controller = controller
        self.resources = resource_tree(controller)
        if not self.resources:
            raise ValueError("Application has no executable Harness resources")
        self.surface = _surface(controller, self.resources)
        self.substrate = SimpleNamespace(name="pi", surface=lambda: self.surface)
        channel = ChannelSpec(kind="pi", model="configured-application-roles")
        self.spec = BenchSpec(name="agentloom", runs_root=controller.store.root / "method",
            install_mode="workspace_config", prompts=BenchPrompts(
                work="Complete the original Application task using its configured Agents, tools and Skills.",
                domain_word="application", prompt_pack="tts", feedback_mode=controller.policy.feedback.mode),
            channels={"proposer": channel, "selector": channel}, allow_subagents=True)
        self.base_dir = controller.store.root / "harness-base"
        if not self.base_dir.exists():
            _write_resources(self.resources, self.base_dir)
        elif {path.relative_to(self.base_dir).as_posix(): _read(path)
              for path in self.base_dir.rglob("*") if path.is_file()} != self.resources:
            raise ValueError("Frozen Harness base resources changed on recovery")
        self.materials = controller.store.root / "materials"
        self.materials.mkdir(exist_ok=True)
        materializer = getattr(controller.driver, "task_materials", None)
        if materializer:
            for name, value in materializer(controller.invocation).items():
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Task materials require safe relative paths")
                _write_resources({name: value}, self.materials)
        else:
            for index, source in enumerate(controller.invocation.request.input_paths):
                target = self.materials / f"input-{index}"
                if not target.exists():
                    shutil.copytree(source, target) if source.is_dir() else shutil.copyfile(source, target)

    def tasks(self, split="all"):
        return [self.task_id]

    def domain_of(self, task_id):
        return self.controller.request.application_id

    def task_prompt(self, task_id):
        invocation = self.controller.invocation
        return invocation.objective + "\n\nOriginal task:\n" + json.dumps(json_value(invocation.request.task), ensure_ascii=False)

    def task_files(self, task_id):
        return self.materials

    def base_harness(self):
        return self.base_dir

    def _candidate(self, tree):
        changes = {name: tree.get(name) for name in self.resources.keys() | tree.keys()
                   if self.resources.get(name) != tree.get(name)}
        if not changes:
            return self.controller.base
        return derive_candidate(self.controller.base, changes, self.controller.store.root / "bundles",
            editable=self.controller.policy.editable, validate=self.controller._validate,
            permitted_paths=tuple(self.resources))

    def validate_tree(self, tree):
        self._candidate(tree)

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        from harness2.core.tree import read_tree
        controller = self.controller
        label = run_id.rsplit("/", 1)[-1]
        bundle = controller.base if harness_dir is None else self._candidate(read_tree(self.surface, harness_dir))
        receipt = controller._trial(label, bundle)
        root = controller.store.root / "trials" / label
        path = root / "receipt.json"
        # Full host receipt is never copied as a model-facing artifact.
        controller.store.write(str(path.relative_to(controller.store.root)), receipt)
        return root

    def _receipt(self, run_dir):
        receipt = ExecutionReceipt.from_dict(json.loads((Path(run_dir) / "receipt.json").read_text()))
        record = self.controller.store.read("stages/" + receipt.trial_id + ".json")
        if not record or record.get("status") != "completed" or record.get("result") != receipt.to_dict():
            raise ValueError("Trial receipt differs from its completed host execution record")
        return receipt

    def delivered(self, run_dir):
        try:
            return self._receipt(run_dir).eligible and self.artifacts_intact(run_dir)
        except (OSError, ValueError):
            return False

    def artifacts_intact(self, run_dir):
        return artifacts_intact(self._receipt(run_dir))

    def _public_output(self, run_dir):
        receipt = self._receipt(run_dir)
        return json_value(receipt.output)

    def deliverable_text(self, run_dir):
        return json.dumps(self._public_output(run_dir), ensure_ascii=False, indent=2) if self.delivered(run_dir) else ""

    def deliverable_files(self, run_dir):
        return {"application-output.json": self.deliverable_text(run_dir)} if self.delivered(run_dir) else {}

    def trajectory(self, run_dir, *, obs_trunc):
        receipt = self._receipt(run_dir)
        renderer = getattr(self.controller.driver, "evidence", None)
        evidence = renderer(self.controller.invocation, receipt) if renderer else {
            "status": receipt.status, "accepted": receipt.accepted,
            "acceptance": receipt.acceptance, "output": receipt.output, "error": receipt.error}
        lines = ["[OBSERVATION]\n" + json.dumps(json_value(evidence), ensure_ascii=False, indent=2)]
        # The driver summary is navigation, not a substitute for actual tool
        # behavior. Retain every recorded action and observation, truncating
        # only individual observations under the upstream display contract.
        boundary = Path(run_dir).resolve()
        seen = set()
        for run in receipt.runs:
            manifest = run.get("manifest_path")
            if not manifest:
                continue
            path = Path(manifest).parent / "audit/runtime_events.jsonl"
            if not path.resolve().is_relative_to(boundary) or path in seen or not path.is_file():
                continue
            seen.add(path)
            lines.append("\n## Runtime " + str(run.get("run_id", path.parent.parent.name)))
            for raw in path.read_text().splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("kind") != "tool":
                    continue
                details = event.get("details", {})
                record = details.get("record") or {}
                name = record.get("tool_name") or details.get("name") or "tool"
                arguments = record.get("input", details.get("arguments", {}))
                lines.append("[ACTION] " + name + "\n" + json.dumps(arguments, ensure_ascii=False))
                value = record.get("output", details.get("result"))
                if record.get("error") or details.get("status") == "failed":
                    lines.append("[ERROR] " + str(record.get("error") or details.get("error", "tool failed")))
                elif value is not None:
                    text = json.dumps(value, ensure_ascii=False)
                    if len(text) > obs_trunc:
                        text = text[:obs_trunc] + "\n[observation display truncated; original retained in host runtime audit]"
                    lines.append("[OBSERVATION]\n" + text)
        return "\n".join(lines)

    def development_feedback(self, task_id, run):
        from .controller import EvaluationPending
        label = run.run_id.rsplit("/", 1)[-1]
        receipt = self._receipt(run.run_dir)
        if label not in self.controller.evaluations:
            try:
                self.controller._evaluate(label, receipt)
            except Exception as error:
                raise EvaluationPending(f"Development evaluator requires recovery for {label}: {error}") from error
        raw = json_value(self.controller.evaluations[label])
        # Evaluators may return arbitrary diagnostic metadata. Only numeric
        # measurements cross the anonymous selector boundary; receipt identities,
        # artifact paths and textual summaries remain in the trusted host record.
        def numeric(value):
            if isinstance(value, dict):
                return {key: item for key, child in value.items()
                        if (item := numeric(child)) is not None and not key.endswith(("_path", "_id"))}
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            return None
        metrics = numeric(raw.get("metrics", {key: value for key, value in raw.items()
                    if key not in {"usage", "partition", "sample_count", "evaluator"}})) or {}
        samples = raw.get("sample_count", 0)
        if type(samples) is not int or samples < 0:
            raise ValueError("Development evaluator sample_count must be a nonnegative integer")
        unscorable = raw.get("status") == "unscorable" or raw.get("accepted") is False or not metrics
        return {"status": "unscorable" if unscorable else "scored", "metrics": metrics,
                "sample_count": samples, "partition": self.controller.policy.feedback.partition,
                "evaluator": self.controller.policy.feedback.evaluator,
                "reason": "No eligible measurement samples" if unscorable else ""}



@contextmanager
def _stream_library(controller, adapter, mode):
    import fcntl
    from harness2.core.library import Library
    stream = json_value(controller.request.harness_stream)
    if stream:
        if set(stream) != {"id", "task_ids", "task_id", "root"}:
            raise ValueError("Harness stream requires id, task_ids, task_id and root")
        tasks = stream["task_ids"]
        if not isinstance(tasks, list) or not tasks or len(tasks) != len(set(tasks)):
            raise ValueError("Harness stream requires unique ordered task IDs")
        for value in [stream["id"], stream["task_id"], *tasks]:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
                raise ValueError("Harness stream identities must be safe path segments")
        if stream["task_id"] not in tasks:
            raise ValueError("Current task is not in the Harness stream")
        root = Path(stream["root"])
        if not root.is_absolute() or root.resolve() in {Path("/"), Path.home().resolve()}:
            raise ValueError("Harness stream root must be an explicit trusted state directory")
        adapter.task_id = stream["task_id"]
        prior = tasks[:tasks.index(adapter.task_id)]
        stream_id = stream["id"]
    else:
        root, stream_id = controller.store.root / "library", controller.experiment_id
        adapter.task_id, prior = "task", []
    identity = {"application": controller.request.application_id, "stream": stream_id, "mode": mode,
                "k": controller.policy.recursion.k, "feedback": json_value(controller.policy.feedback.model_dump()),
                "revision": METHOD_REVISION}
    namespace = fingerprint(identity)
    location = root / namespace
    location.mkdir(parents=True, exist_ok=True)
    with (location / "library.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        from .store import ExperimentStore
        stream_store = ExperimentStore(location)
        try:
            stream_store.immutable("stream.json", {"identity": identity,
                "task_ids": tasks if stream else [adapter.task_id],
                "method": method_identity(),
                "evaluator": evaluator_identity(controller.policy.feedback.evaluator)})
            bindings = stream_store.read("task-bindings.json", {})
            binding = {"task_snapshot_id": controller.invocation.task_snapshot_id,
                "base_candidate_id": controller.base.candidate_id,
                "model_profile_id": controller.model_profile_id,
                "experiment_root": str(controller.store.root)}
            previous = bindings.get(adapter.task_id)
            if previous is not None and previous != binding:
                raise ValueError("Harness stream task belongs to different frozen inputs, configuration or execution")
            ordered = tasks if stream else [adapter.task_id]
            current_index = ordered.index(adapter.task_id)
            if any(ordered.index(task) > current_index for task in bindings):
                raise ValueError("Cannot replay an earlier incomplete task after this Harness stream advanced")
            for task in prior:
                earlier = bindings.get(task)
                if earlier is None:
                    raise ValueError("Harness stream cannot skip an earlier task")
                completed = Path(earlier["experiment_root"]) / "result.json"
                if not completed.is_file():
                    raise ValueError("Earlier Harness stream task has no completed delivery")
                result = json.loads(completed.read_text())
                if result.get("status") != "completed" or result.get("task_snapshot_id") != earlier["task_snapshot_id"]:
                    raise ValueError("Earlier Harness stream task requires recovery before advancing")
            bindings[adapter.task_id] = binding
            stream_store.write("task-bindings.json", bindings)
            library = Library(location, controller.request.application_id, adapter.surface)
            library.retain(prior)
            yield library
        finally:
            stream_store.close()



def run_method(controller):
    from harness2.core.proposer import Proposer
    from harness2.core.schedule import mode_token, run_task
    adapter = ApplicationAdapter(controller)
    mode = mode_token(controller.policy.recursion.mode)
    prop = Proposer(adapter.spec.channels["proposer"], audit_dir=controller.store.root / "method" / "sessions",
                    transport=PiTransport(controller))
    with _stream_library(controller, adapter, mode) as library:
        result = run_task(adapter.task_id, controller.policy.recursion.k, adapter=adapter, prop=prop,
                          runs_root=adapter.spec.runs_root, modes=(mode,), library=library, selector_prop=prop,
                          tag=fingerprint([controller.experiment_id, json_value(controller.policy.feedback.model_dump())])[:16])
        # A cached upstream task does not call execute. Rehydrate only host
        # receipts; completed business work is never replayed to build a report.
        from .snapshot import load_bundle
        for path in sorted((controller.store.root / "trials").glob("*/receipt.json")):
            receipt = ExecutionReceipt.from_dict(json.loads(path.read_text()))
            if receipt.trial_id not in controller.receipts:
                controller.receipts[receipt.trial_id] = receipt
                controller.trials.append(receipt)
                controller.bundles[receipt.trial_id] = load_bundle(controller.store.root / "bundles" / receipt.candidate_id.split(":", 1)[1])
                controller._usage(receipt.usage)
        records = {item["stage"]: item for item in controller.role_receipts}
        for path in sorted((controller.store.root / "roles").glob("*/receipt.json")):
            record = json.loads(path.read_text())
            records[record["stage"]] = record
            controller._usage(record.get("receipt", {}).get("usage_observations", ()))
        controller.role_receipts = list(records.values())
        result.mode = mode
        result.library_result = {"submitted": int(adapter.task_id in library.entries()),
                                 "entries": library.entries(), "path": str(library.dir)}
        return result
