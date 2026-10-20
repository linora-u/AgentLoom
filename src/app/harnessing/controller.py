"""One persistent improvement loop around a finite, complete Application task.

Business execution and improvement roles use the same physical process boundary.
The controller owns decisions; a candidate never owns its judge or task contract.
"""
from __future__ import annotations

import importlib
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .models import (
    ApplicationExecutionResult,
    ApplicationRequest,
    ExecutionReceipt,
    PreparedInvocation,
    TrialContext,
    UsageObservation,
    bind_trial_context,
    current_trial_context,
    fingerprint,
    json_value,
)
from .roles import candidate_files
from .store import ExperimentStore


class ExperimentStopped(KeyboardInterrupt):
    """A stopped experiment may be resumed after owned resources are reconciled."""


def load_driver(reference: str):
    module, separator, symbol = reference.partition(":")
    if not separator or not module or not symbol.isidentifier():
        raise ValueError("Driver/evaluator must be a registered importable module:symbol")
    value = getattr(importlib.import_module(module), symbol)
    return value() if isinstance(value, type) else value


def execute_trial(payload: dict, bundle: dict, context: dict) -> dict:
    """Importable trusted process factory used by both built-in drivers."""
    from .snapshot import load_bundle

    archived = load_bundle(Path(bundle["root"]))
    trial = TrialContext.from_dict(context)
    invocation = PreparedInvocation.from_dict(payload["invocation"])
    from agentloom.config.config import bind_config, load_project_config

    with bind_trial_context(trial), bind_config(load_project_config(archived.project_root)):
        driver = load_driver(payload["driver"])
        reconcile = getattr(driver, "reconcile", None)
        result = reconcile(invocation, archived, trial) if reconcile else None
        if result is None:
            result = driver.execute_once(invocation, archived, trial)
    result = result if isinstance(result, ExecutionReceipt) else ExecutionReceipt.from_dict(result)
    if result.trial_id != trial.trial_id or result.candidate_id != trial.candidate_id:
        raise ValueError("Driver returned an unrelated execution receipt")
    return result.to_dict()


def _policy_dict(policy):
    if hasattr(policy, "to_dict"):
        return policy.to_dict()
    if hasattr(policy, "model_dump"):
        return policy.model_dump(mode="json")
    from dataclasses import asdict

    return asdict(policy)


def _copy_inputs(paths, root: Path, *, hidden_paths=()):
    """Freeze supplied facts once; no live source path is granted to trial code."""
    import hashlib
    import os
    import stat
    from agentloom.execution.storage import SecureDirectory

    manifest, replacements = {}, []
    with SecureDirectory(root) as storage:
        for index, raw in enumerate(paths):
            source = Path(raw)
            if source.is_symlink():
                raise ValueError("Input snapshots cannot follow symlinks")
            source = source.resolve(strict=True)
            if any(
                source.is_relative_to(Path(hidden).resolve()) or Path(hidden).resolve().is_relative_to(source)
                for hidden in hidden_paths
            ):
                raise ValueError("Task input root overlaps hidden evaluation data; prepare a public projection")
            logical_root = f"input-{index}"
            destination = root / logical_root
            files = sorted(source.rglob("*")) if source.is_dir() else [source]
            if source.is_dir():
                storage.ensure_dir(logical_root)
            for path in files:
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError("Input snapshot contains a link or special file")
                if path.is_dir():
                    continue
                relative = Path(logical_root) / path.relative_to(source) if source.is_dir() else Path(logical_root)
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("Input snapshot source is not a regular file")
                    copied = stream.read()
                    after = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("Input source changed while freezing the task")
                storage.atomic_write(relative, copied)
                manifest[relative.as_posix()] = hashlib.sha256(copied).hexdigest()
            replacements.append((source, destination))
    return manifest, replacements


def _verify_inputs(invocation, root):
    import hashlib

    expected = dict(invocation.inputs.get("frozen_files", {}))
    observed = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Frozen input contains a link or special file")
        if path.is_file():
            observed[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != expected:
        raise ValueError("Frozen task input bytes changed on recovery")


def _rebase(value, replacements):
    value = json_value(value)
    if isinstance(value, dict):
        return {key: _rebase(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebase(item, replacements) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        for source, destination in replacements:
            try:
                return str(destination / Path(value).relative_to(source))
            except ValueError:
                pass
    return value


class ImprovementController:
    def __init__(
        self,
        request,
        policy,
        store,
        *,
        events=None,
        stop_token=None,
        environment=None,
        model_config=None,
        credentials=None,
        library=None,
    ):
        self.request, self.policy, self.store = request, policy, store
        self.driver = load_driver(request.driver)
        self.events, self.stop = events, stop_token
        self.environment, self.model_config = environment, model_config
        self.credentials = dict(credentials or {})
        self.model_profile_id = ""
        self.public_models = {}
        self.library = library
        self.trials, self.role_receipts, self.errors = [], [], []
        self.bundles, self.receipts, self.evaluations = {}, {}, {}
        self._mutex = threading.RLock()
        self.experiment_id = store.root.name
        self.invocation = None
        self.library_view, self.memory_snapshot = (), ""

    def _check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise ExperimentStopped("Application experiment stopped")

    def _emit(self, event, **details):
        if self.events is not None:
            try:
                self.events({"event": event, "experiment_id": self.experiment_id, **json_value(details)})
            except Exception:
                pass

    def _context(self, key, bundle, *, role=False):
        return TrialContext(
            self.experiment_id,
            key,
            bundle.candidate_id,
            self.invocation.task_snapshot_id,
            self.store.root / ("roles" if role else "trials") / key,
            memory_snapshot=self.memory_snapshot,
            library_view=self.library_view if role else (),
        )

    def _process(self, bundle, context, factory, payload):
        from .process import execute_isolated

        execute = execute_isolated
        if (
            factory == "agentloom.app.harnessing.controller:execute_trial"
            and self.request.driver == "news_agent.runtime.harnessing:NewsApplicationDriver"
        ):
            # A fixed in-tree adapter keeps DBOS arbitration outside its Host
            # sandboxes. No candidate configuration can request this boundary.
            from .process import execute_trusted_orchestration

            execute = execute_trusted_orchestration

        self._check_stop()
        options = {
            "stop": self.stop,
            "read_paths": self.invocation.request.input_paths,
            "environment": self.environment,
            "denied_paths": self.request.hidden_paths,
            "credentials": self.credentials,
        }
        if self.model_config is not None:
            options["model_config"] = self.model_config
        try:
            return execute(bundle, context, factory, json_value(payload), **options)
        except BaseException as error:
            # Operation-level unknowns are not fabricated zero-token model
            # invocations. They survive retries alongside any measured calls.
            with self._mutex:
                unknown = self.store.read("unmeasured-operations.json", {})
                attempt = len(tuple((context.root / "process/attempts").glob("*")))
                key = f"{context.trial_id}:{attempt}"
                unknown[key] = {
                    "operation_id": context.trial_id,
                    "attempt": attempt,
                    "factory": factory,
                    "error_type": type(error).__name__,
                    "usage": "unknown",
                }
                self.store.write("unmeasured-operations.json", unknown)
            if isinstance(error, InterruptedError):
                raise ExperimentStopped(str(error)) from error
            raise

    def _usage(self, observations):
        with self._mutex:
            summary = self.store.observe_usage(observations)
            unknown = self.store.read("unmeasured-operations.json", {})
            summary["unmeasured_operations"] = [unknown[key] for key in sorted(unknown)]
            summary["complete"] = summary["complete"] and not unknown
            return summary

    def _trial(self, key, bundle):
        self._check_stop()
        context = self._context(key, bundle)
        intent = {
            "context": context,
            "invocation": self.invocation,
            "driver": self.request.driver,
            "bundle": bundle.to_dict(),
        }
        record = self.store.begin(key, intent)
        if record["status"] == "completed":
            receipt = ExecutionReceipt.from_dict(record["result"])
        else:
            self._emit("trial.started", trial_id=key, candidate_id=bundle.candidate_id)
            result = self._process(
                bundle,
                context,
                "agentloom.app.harnessing.controller:execute_trial",
                {"driver": self.request.driver, "invocation": self.invocation.to_dict()},
            )
            receipt = ExecutionReceipt.from_dict(result)
            if receipt.trial_id != key or receipt.candidate_id != bundle.candidate_id:
                raise ValueError("Physical trial returned a different identity")
            self.store.finish(key, receipt)
        with self._mutex:
            self.trials.append(receipt)
            self.receipts[key], self.bundles[key] = receipt, bundle
        self._usage(receipt.usage)
        self._emit("trial.completed", trial_id=key, status=receipt.status, accepted=receipt.accepted)
        if self.policy.feedback.mode == "development_score":
            self._evaluate(key, receipt)
        return receipt

    def _evaluate(self, key, receipt):
        evaluation_key = "evaluation." + key
        reference = self.policy.feedback.evaluator
        partition = self.policy.feedback.partition or ""
        record = self.store.begin(
            evaluation_key,
            {"evaluator": reference, "partition": partition, "invocation": self.invocation, "receipt": receipt},
        )
        if record["status"] == "completed":
            result = record["result"]
        else:
            self._check_stop()
            evaluator = load_driver(reference)
            evaluate = getattr(evaluator, "evaluate", evaluator)
            result = json_value(evaluate(self.invocation, receipt, partition))
            if not isinstance(result, dict):
                raise ValueError("Development evaluator must return a JSON mapping")
            if result.get("partition", partition) != partition:
                raise ValueError("Evaluator returned a different data partition")
            self.store.finish(evaluation_key, result)
        self.evaluations[key] = result
        self._usage(result.get("usage", ()))

    def _evidence(self, current, *, pool=()):
        evidence = {
            "objective": self.invocation.objective,
            "request": self.invocation.request.task,
            "inputs": self.invocation.inputs,
            "acceptance": self.invocation.acceptance,
            "feedback": self.policy.feedback.mode,
            "files": candidate_files(current),
            "baseline": [
                self._render_receipt(self.receipts[key]) for key in sorted(self.receipts) if key.startswith("base")
            ],
            "library": self.library_view,
            "pool": [
                {
                    "label": key,
                    "files": candidate_files(self.bundles[key]),
                    "execution": self._render_receipt(self.receipts[key]),
                }
                for key in pool
            ],
        }
        return json_value(evidence)

    def _render_receipt(self, receipt):
        renderer = getattr(self.driver, "evidence", None)
        value = (
            renderer(self.invocation, receipt)
            if renderer
            else {
                "trial_id": receipt.trial_id,
                "status": receipt.status,
                "output": receipt.output,
                "accepted": receipt.accepted,
                "acceptance": receipt.acceptance,
                "error": receipt.error,
            }
        )
        value = json_value(value)
        if self.policy.feedback.mode == "development_score":
            value = {"execution": value, "development_evaluation": self.evaluations.get(receipt.trial_id)}
        return value

    def _role(self, key, role, bundle, evidence):
        self._check_stop()
        # Improvement role definitions are controller policy, frozen at the
        # task base. Application edits never rewrite their own selector/judge.
        bundle = self.base
        definition = getattr(self.policy.roles, role)
        payload = {"stage": key, "role": role, "definition": definition, "evidence": evidence}
        context = self._context(key, bundle, role=True)
        record = self.store.begin(key, {"context": context, "payload": payload, "bundle": bundle.to_dict()})
        if record["status"] == "completed":
            result = record["result"]
        else:
            self._emit("role.started", stage=key, role=role)
            result = self._process(bundle, context, "agentloom.app.harnessing.roles:execute_role", payload)
            self.store.finish(key, result)
        with self._mutex:
            self.role_receipts.append({"stage": key, "role": role, **result})
        actual = result.get("receipt", {})
        invocation_id = actual.get("invocation_id")
        if actual.get("usage_observations"):
            self._usage(actual["usage_observations"])
        elif invocation_id:
            usage = actual.get("usage", {})
            self._usage(
                (
                    UsageObservation(
                        invocation_id=invocation_id,
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        total_tokens=usage.get("total_tokens"),
                        complete=False,
                        duration_seconds=actual.get("duration_seconds"),
                    ),
                )
            )
        self._emit("role.completed", stage=key, role=role, status=result.get("status"))
        self._check_stop()
        if result.get("status") != "completed":
            raise ValueError(f"{role} failed: {result.get('error', 'no output')}")
        return result["output"], actual

    def _edit(self, label, role, current, evidence):
        from .snapshot import derive_candidate, load_bundle

        record = self.store.begin(
            "candidate." + label, {"parent": current.candidate_id, "role": role, "evidence": evidence}
        )
        if record["status"] == "completed":
            bundle = load_bundle(Path(record["result"]["root"]))
            bundle.verify()
            return bundle
        error = ""
        for attempt in (1, 2):
            self._check_stop()
            try:
                output, _receipt = self._role(
                    f"{label}.edit{attempt}",
                    role,
                    current,
                    {**evidence, **({"previous_error": error} if error else {})},
                )
                changes = output.get("edits")
                if not isinstance(changes, Mapping):
                    raise ValueError("Role must return an explicit edit mapping")
                bundle = derive_candidate(
                    current,
                    changes,
                    self.store.root / "bundles",
                    editable=self.policy.editable,
                    validate=self._validate,
                )
                self.store.finish(
                    "candidate." + label,
                    {
                        "root": str(bundle.root),
                        "candidate_id": bundle.candidate_id,
                        "rationale": output.get("rationale", ""),
                        "inherited": False,
                    },
                )
                return bundle
            except Exception as exc:
                if getattr(exc, "category", "") == "infrastructure":
                    raise
                error = f"{type(exc).__name__}: {exc}"
        # Upstream edit-stage fallback inherits the current configuration,
        # but the resulting probe/final still runs as a fresh full trial.
        self.store.finish(
            "candidate." + label,
            {"root": str(current.root), "candidate_id": current.candidate_id, "inherited": True, "error": error},
        )
        return current

    def _independent(self, functions):
        with ThreadPoolExecutor(max_workers=min(2, len(functions))) as pool:
            return list(pool.map(lambda function: function(), functions))

    def _parallel(self, base, k):
        common = self._evidence(base)
        jobs = [(f"{role}{index}", role) for index in range(1, k + 1) for role in ("general", "domain")]
        proposed = self._independent(
            [lambda label=label, role=role: self._edit(label, role, base, common) for label, role in jobs]
        )
        self._independent(
            [
                lambda label=label, bundle=bundle: self._trial(label, bundle)
                for (label, _), bundle in zip(jobs, proposed)
            ]
        )
        labels = [label for label, _ in jobs]
        evidence = self._evidence(base, pool=labels)
        finals = [f"final{index}" for index in range(1, k + 1)]
        composed = self._independent(
            [lambda label=label: self._edit(label, "compose", base, evidence) for label in finals]
        )
        self._independent(
            [lambda label=label, bundle=bundle: self._trial(label, bundle) for label, bundle in zip(finals, composed)]
        )
        return finals

    def _sequential(self, base, k):
        current, history, finals = base, [], []
        for index in range(1, k + 1):
            common = self._evidence(current, pool=history)
            jobs = [(f"{role}{index}", role) for role in ("general", "domain")]
            proposed = self._independent(
                [lambda label=label, role=role: self._edit(label, role, current, common) for label, role in jobs]
            )
            self._independent(
                [
                    lambda label=label, bundle=bundle: self._trial(label, bundle)
                    for (label, _), bundle in zip(jobs, proposed)
                ]
            )
            history.extend(label for label, _ in jobs)
            label = f"final{index}"
            current = self._edit(label, "compose", current, self._evidence(current, pool=history))
            self._trial(label, current)
            history.append(label)
            finals.append(label)
        return finals

    def _select(self, finals):
        eligible = [key for key in finals if self.receipts[key].eligible]
        if not eligible:
            raise ValueError("No composed final satisfies the complete business delivery contract")
        labels = sorted(eligible)
        random.Random(
            fingerprint([self.invocation.task_snapshot_id, self.policy.recursion.mode, self.policy.recursion.k])
        ).shuffle(labels)
        choices = {f"C{index}": label for index, label in enumerate(labels, 1)}
        intent = {"eligible": choices, "receipts": [self.receipts[key] for key in labels]}
        record = self.store.begin("selection", intent)
        if record["status"] == "completed":
            result = record["result"]
        elif self.policy.recursion.k == 1:
            result = {"chosen": labels[0], "reason": "single accepted composed final; no ranking call", "trivial": True}
            self.store.finish("selection", result)
        else:
            output, _receipt = self._role(
                "selection.rank",
                "select",
                self.base,
                {
                    "objective": self.invocation.objective,
                    "acceptance": self.invocation.acceptance,
                    "inputs": self.invocation.inputs,
                    "feedback": self.policy.feedback.mode,
                    "candidates": {
                        anonymous: self._selection_evidence(self.receipts[label])
                        for anonymous, label in choices.items()
                    },
                },
            )
            if output.get("chosen_id") not in choices:
                raise ValueError("Selector chose an unavailable final")
            result = {"chosen": choices[output["chosen_id"]], "reason": output["reason"], "trivial": False}
            self.store.finish("selection", result)
        return result

    def _selection_evidence(self, receipt):
        # Only blinded output/acceptance/allowed evaluation, without branch or
        # runtime identifiers. Trusted driver evidence may include artifact paths.
        value = {
            "status": receipt.status,
            "output": receipt.output,
            "accepted": receipt.accepted,
            "acceptance": receipt.acceptance,
        }
        if self.policy.feedback.mode == "development_score":
            value["development_evaluation"] = self.evaluations.get(receipt.trial_id)
        return json_value(value)

    @contextmanager
    def _private_bindings(self):
        from agentloom.config.config import bind_harness_model_config
        from agentloom.config.private_bindings import bind_harness_credentials

        with bind_harness_model_config(self.model_config), bind_harness_credentials(self.credentials):
            yield

    def _validate(self, project, app):
        with self._private_bindings():
            self.driver.validate_candidate(self.invocation, project, app)

    def _freeze_models(self):
        # Store transport/model parameters and private references, never keys.
        # A resumed task keeps these parameters even if live defaults change.
        from .snapshot import _sanitize

        private = {}
        public = _sanitize(json_value(self.model_config), "models", [], credentials=private)
        frozen_catalog = self.store.read("model-profiles.json")
        if frozen_catalog is None:
            frozen_catalog = self.store.immutable("model-profiles.json", public)
        prefix = "harness-credential-ref:"

        def bind(value, *, remove=False):
            if isinstance(value, str) and value.startswith(prefix):
                reference = value[len(prefix) :]
                if remove:
                    return None
                if reference not in private:
                    raise ValueError("A frozen model profile is missing its trusted credential")
                return private[reference]
            if isinstance(value, dict):
                return {key: bind(item, remove=remove) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item, remove=remove) for item in value]
            return value

        self.model_config = bind(frozen_catalog)
        self.public_models = bind(frozen_catalog, remove=True)
        self.model_profile_id = fingerprint(frozen_catalog)

    def _restore_credentials(self):
        """Rebind only frozen capability references; never archive private values."""
        import json
        from agentloom.config.yaml_loader import load_unique_yaml

        for reference in self.base.manifest.get("credential_refs", ()):
            if reference in self.credentials:
                continue
            logical, separator, pointer = reference.partition("#")
            source = self.request.project_root / logical
            if not separator or source.is_symlink() or not source.resolve().is_relative_to(self.request.project_root):
                raise ValueError("Frozen credential reference has no trusted source")
            raw = source.read_text()
            value = json.loads(raw) if source.suffix == ".json" else load_unique_yaml(raw)
            for part in pointer.strip("/").split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
            self.credentials[reference] = value
        if set(self.credentials) - set(self.base.manifest.get("credential_refs", ())):
            raise ValueError("Trusted credentials exceed the frozen capability references")

    def prepare(self):
        from .snapshot import snapshot_application, load_bundle
        from .isolation import preflight_isolation

        preflight_isolation()
        for method in ("prepare", "execute_once", "reconcile", "publish", "validate_candidate", "preflight"):
            if not callable(getattr(self.driver, method, None)):
                raise ValueError(f"Application driver lacks required {method} capability")
        self.driver.preflight(self.request, self.policy)
        if self.policy.feedback.mode == "development_score":
            load_driver(self.policy.feedback.evaluator)
        self.store.immutable("request.json", self.request)
        self.store.immutable("policy.json", _policy_dict(self.policy))
        self._freeze_models()
        base_record = self.store.read("base.json")
        if base_record:
            self.base = load_bundle(Path(base_record["root"]))
            self.base.verify()
        else:
            resources = getattr(self.driver, "editable_resources", lambda request: {})(self.request)
            self.base = snapshot_application(
                self.request.project_root,
                self.request.application_root,
                self.store.root / "bundles",
                excluded_paths=self.request.hidden_paths,
                editable_resources=resources,
                credential_sink=self.credentials,
                model_config=self.public_models,
            )
            self.store.immutable("base.json", self.base.to_dict())
        self._restore_credentials()
        prepared = self.store.read("prepared.json")
        if prepared:
            self.invocation = PreparedInvocation.from_dict(prepared)
        else:
            with self._private_bindings():
                invocation = self.driver.prepare(self.request, self.base)
            if not isinstance(invocation, PreparedInvocation):
                invocation = PreparedInvocation.from_dict(invocation)
            manifest, replacements = _copy_inputs(
                invocation.request.input_paths, self.store.root / "inputs", hidden_paths=self.request.hidden_paths
            )
            request = replace(
                invocation.request,
                input_paths=tuple(target for _, target in replacements),
                task=_rebase(invocation.request.task, replacements),
                driver_options=_rebase(invocation.request.driver_options, replacements),
            )
            self.invocation = PreparedInvocation(
                request,
                invocation.objective,
                {
                    **_rebase(invocation.inputs, replacements),
                    "frozen_files": manifest,
                    "model_profile_id": self.model_profile_id,
                },
                invocation.acceptance,
                _rebase(invocation.data, replacements),
            )
            self.store.immutable("prepared.json", self.invocation)
        _verify_inputs(self.invocation, self.store.root / "inputs")
        self._validate(self.base.project_root, self.base.application_root)
        view = self.store.read("library-view.json")
        if view is None:
            feedback, partition = self.policy.feedback.mode, self.policy.feedback.partition or ""
            entries = (
                self.library.view(
                    feedback=feedback, partition=partition, cutoff=str(self.invocation.data.get("cutoff", ""))
                )
                if self.library
                else ()
            )
            # Explicit reference text is fixed here; role/business Runtime
            # injection must never re-read the shared live MemoryStore.
            snapshot = getattr(self.driver, "memory_snapshot", lambda invocation: "")(self.invocation)
            if not isinstance(snapshot, str):
                raise ValueError("Driver memory snapshot must be fixed text")
            view = {"entries": entries, "memory_snapshot": snapshot}
            self.store.immutable("library-view.json", view)
        self.library_view, self.memory_snapshot = tuple(view["entries"]), view["memory_snapshot"]

    def run(self):
        self.prepare()
        old = self.store.read("result.json")
        if old and old["status"] == "completed":
            return ApplicationExecutionResult.from_dict(old)
        selected, delivery, reason, library_result = None, {}, "", {}
        status = "failed"
        try:
            self._check_stop()
            k = self.policy.recursion.k
            for index in range(1, k + 1):
                self._trial(f"base{index}", self.base)
            if not any(receipt.status == "completed" for receipt in self.trials):
                raise ValueError("No baseline execution produced usable task evidence")
            finals = (
                self._parallel(self.base, k)
                if self.policy.recursion.mode == "parallel"
                else self._sequential(self.base, k)
            )
            choice = self._select(finals)
            selected = self.receipts[choice["chosen"]]
            reason = choice["reason"]
            decision_id = fingerprint([self.experiment_id, selected.trial_id, selected.candidate_id])
            publication = self.store.begin("publication", {"selected": selected, "decision_id": decision_id})
            if publication["status"] == "completed":
                delivery = publication["result"]
            else:
                delivery = json_value(self.driver.publish(self.invocation, selected, decision_id))
                self.store.finish("publication", delivery)
            status = "completed"
            try:
                evidence = self._evidence(self.bundles[selected.trial_id], pool=sorted(self.receipts))
                evidence.update(
                    selected_trial=selected.trial_id, selection_reason=reason, evidence_refs=sorted(self.receipts)
                )
                output, reflection_receipt = self._role(
                    "reflection", "reflect", self.bundles[selected.trial_id], evidence
                )
                entry = self.store.begin("library", {"selected": selected, "reflection": output})
                if entry["status"] == "completed":
                    library_result = entry["result"]
                else:
                    experiences = output.get("experiences", [])
                    valid_refs = set(self.receipts)
                    if any(set(item.get("evidence_refs", ())) - valid_refs for item in experiences):
                        raise ValueError("Reflection references unknown execution evidence")
                    library_result = (
                        self.library.submit(
                            experiences,
                            selected=selected,
                            bundle=self.bundles[selected.trial_id],
                            invocation=self.invocation,
                            experiment_id=self.experiment_id,
                            reflection_run_id=reflection_receipt.get("root_run_id")
                            or reflection_receipt.get("run", {}).get("run_id", ""),
                            feedback=self.policy.feedback.mode,
                            partition=self.policy.feedback.partition or "",
                            available_at=datetime.now(timezone.utc).isoformat(),
                        )
                        if self.library
                        else {"submitted": 0}
                    )
                    self.store.finish("library", library_result)
            except Exception as error:
                self.errors.append(f"Reflection/library failed after delivery: {type(error).__name__}: {error}")
                status = "partial"
        except (KeyboardInterrupt, ExperimentStopped) as error:
            self.errors.append(str(error) or "Application experiment stopped")
            status = "stopped"
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
            status = "partial" if delivery else "failed"
        result = ApplicationExecutionResult(
            self.experiment_id,
            self.invocation.task_snapshot_id,
            status,
            selected.output if selected else None,
            selected.trial_id if selected else "",
            selected.candidate_id if selected else "",
            reason,
            delivery,
            tuple(sorted(self.trials, key=lambda receipt: receipt.trial_id)),
            tuple(sorted(self.role_receipts, key=lambda receipt: receipt["stage"])),
            self._usage(()),
            tuple(self.errors),
            library_result,
        )
        self.store.write("result.json", result)
        self._emit("experiment.completed", result=result)
        return result


def run_application(
    request: ApplicationRequest,
    *,
    policy=None,
    events=None,
    stop_token=None,
    resume_id=None,
    state_root: Path | None = None,
    environment=None,
    model_config=None,
    credentials=None,
    library=None,
) -> ApplicationExecutionResult:
    """Public complete-task entry. Drivers never recursively call this facade."""
    if not isinstance(request, ApplicationRequest):
        request = ApplicationRequest.from_dict(request)
    if current_trial_context() is not None:
        raise ValueError("Nested complete-task dispatch must use the bound driver's once entry")
    if stop_token is not None and not callable(getattr(stop_token, "is_set", None)):
        raise ValueError("stop_token must expose is_set()")
    if policy is None:
        from agentloom.config.config import load_project_config
        from agentloom.app.application import _system_settings
        from .policy import resolve_harnessing_policy

        config = load_project_config(request.project_root)
        app, _effective = _system_settings(config, request.application_root)
        policy = resolve_harnessing_policy(config.raw, app)
    if not policy.enabled:
        driver = load_driver(request.driver)
        ordinary = getattr(driver, "execute_ordinary", None)
        if not callable(ordinary):
            raise ValueError("Application driver requires an ordinary execution entry")
        return ordinary(request, events=events, stop_token=stop_token, resume_id=resume_id)
    if model_config is None:
        from agentloom.config.config import load_project_config

        config = load_project_config(request.project_root)
        catalog = config.llm.overlay_from_yaml(request.application_root / "config/model.yaml")
        catalog = catalog.patch_from_yaml(request.application_root / "config/model.local.yaml")
        model_config = catalog.model_dump(mode="json")
    if state_root is None:
        from agentloom.config.config import load_project_config
        from agentloom.execution.context import resolve_runtime_home

        config = load_project_config(request.project_root)
        state_root = resolve_runtime_home(config.raw, agent_root=request.project_root).root_dir / "harnessing"
    experiment_id = resume_id or "experiment-" + uuid.uuid4().hex
    if not isinstance(experiment_id, str) or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in experiment_id
    ):
        raise ValueError("Invalid experiment resume identity")
    store = ExperimentStore(Path(state_root) / experiment_id)
    try:
        with store.lock():
            if library is None:
                from .library import HarnessLibrary
                from agentloom.config.config import bind_config, load_project_config

                config = load_project_config(request.project_root)
                with bind_config(config):
                    library = HarnessLibrary(request.application_id, agent_config=config.raw)
            return ImprovementController(
                request,
                policy,
                store,
                events=events,
                stop_token=stop_token,
                environment=environment,
                model_config=model_config,
                credentials=credentials,
                library=library,
            ).run()
    finally:
        store.close()
