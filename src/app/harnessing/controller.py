"""One persistent improvement loop around a finite, complete Application task.

Harness² owns method decisions. This adapter owns frozen inputs, trusted business
execution, physical lifecycle, usage observations and idempotent publication.
"""
from __future__ import annotations

import importlib
import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .models import (
    ApplicationExecutionResult,
    ApplicationRequest,
    ExecutionReceipt,
    PreparedInvocation,
    TrialContext,
    bind_trial_context,
    current_trial_context,
    fingerprint,
    json_value,
)
from .store import ExperimentStore


class EvaluationPending(KeyboardInterrupt):
    """Resume the trusted evaluator without repeating completed business work."""


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

            def signature(path):
                item = path.lstat()
                return (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns)

            initial = {path: signature(path) for path in dict.fromkeys([source, *files])}
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
            after_paths = [source, *sorted(source.rglob("*"))] if source.is_dir() else [source]
            if {path: signature(path) for path in after_paths} != initial:
                raise ValueError("Input tree changed while freezing the complete task")
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
        if library is not None:
            raise ValueError("Custom curated Harness libraries are replaced by the upstream experiment Library; use harness_stream")
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

    def _process(self, bundle, context, factory, payload, *, timeout=None, retry_incomplete=True):
        from .process import execute_isolated, execute_managed_orchestration

        # A trusted Python driver may delegate mandatory isolation to managed
        # Hosts. Candidate YAML cannot opt out of the complete-task boundary.
        managed_orchestration = (factory == "agentloom.app.harnessing.controller:execute_trial"
            and getattr(self.driver, "execution_boundary", "isolated") == "managed_hosts")
        if managed_orchestration:
            validator = getattr(self.driver, "validate_managed_execution", None)
            if not callable(validator):
                raise ValueError("Managed-host execution requires a trusted driver validator")
            with self._private_bindings():
                validator(self.invocation, bundle)

        self._check_stop()
        options = {
            "stop": self.stop,
            "timeout": timeout,
            "read_paths": self.invocation.request.input_paths,
            "environment": self.environment,
            "denied_paths": self.request.hidden_paths,
            "credentials": self.credentials,
        }
        if self.model_config is not None:
            options["model_config"] = self.model_config
        if not retry_incomplete:
            options["retry_incomplete"] = False
        try:
            if managed_orchestration:
                return execute_managed_orchestration(bundle, context, factory, json_value(payload), **options)
            return execute_isolated(bundle, context, factory, json_value(payload), **options)
        except BaseException as error:
            # H2 returns/raises only after its owner closes. Salvage persisted
            # observations even if the process died before a driver receipt.
            from .usage import read_run_usage

            for manifest in sorted(context.runtime_root.glob("runs/**/manifest.json")):
                self._usage(read_run_usage(manifest.parent))
            # Operation-level unknowns are not fabricated zero-token model
            # invocations. They survive retries alongside any measured calls.
            with self._mutex:
                unknown = self.store.read("unmeasured-operations.json", {})
                attempt = len(tuple((context.root / "process/attempts").glob("*")))
                key = f"{context.trial_id}:{attempt}"
                unknown.setdefault(key, {
                    "operation_id": context.trial_id,
                    "attempt": attempt,
                    "factory": factory,
                    "error_type": type(error).__name__,
                    "usage": "unknown",
                })
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

    def _register_trial(self, receipt, bundle=None):
        """Register only a receipt matching the authoritative completed stage."""
        record = self.store.read(self.store._stage_name(receipt.trial_id))
        if not record or record.get("status") != "completed" or record.get("result") != receipt.to_dict():
            raise ValueError("Trial receipt differs from its completed host execution record")
        with self._mutex:
            previous = self.receipts.get(receipt.trial_id)
            if previous is not None:
                if previous != receipt:
                    raise ValueError("Registered Trial receipt changed")
                return receipt
            if bundle is None:
                from .snapshot import load_bundle
                bundle = load_bundle(self.store.root / "bundles" / receipt.candidate_id.split(":", 1)[1])
            if receipt.candidate_id != bundle.candidate_id:
                raise ValueError("Trial receipt belongs to another candidate bundle")
            self.trials.append(receipt)
            self.receipts[receipt.trial_id], self.bundles[receipt.trial_id] = receipt, bundle
            self._usage(receipt.usage)
        return receipt

    def restore_trial(self, run_dir):
        """Read cached host evidence through the same registration boundary."""
        run_dir = Path(run_dir).resolve()
        receipt = ExecutionReceipt.from_dict(json.loads((run_dir / "receipt.json").read_text()))
        expected = self.store.root / "trials" / receipt.trial_id
        if run_dir != expected.resolve() or not run_dir.is_relative_to(self.store.root / "trials"):
            raise ValueError("Trial receipt escapes its owned execution state")
        return self._register_trial(receipt)

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
            import hashlib
            artifacts = []
            for raw in receipt.artifacts:
                artifact = json_value(raw)
                if "path" in artifact:
                    path = Path(artifact["path"])
                    if not path.resolve().is_relative_to(context.root) or not path.is_file() or path.is_symlink():
                        raise ValueError("Trial artifact is missing or escapes its owned execution state")
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    if artifact.get("sha256", actual).removeprefix("sha256:") != actual:
                        raise ValueError("Trial artifact changed before its execution receipt was committed")
                    artifact["sha256"] = actual
                artifacts.append(artifact)
            receipt = replace(receipt, artifacts=tuple(artifacts))
            self.store.finish(key, receipt)
        if receipt.trial_id != key or receipt.candidate_id != bundle.candidate_id:
            raise ValueError("Physical trial returned a different identity")
        self._register_trial(receipt, bundle)
        self._emit("trial.completed", trial_id=key, status=receipt.status, accepted=receipt.accepted)
        if self.policy.feedback.mode == "development_score":
            try:
                self._evaluate(key, receipt)
            except Exception as error:
                raise EvaluationPending(f"Development evaluator requires recovery for {key}: {error}") from error
        return receipt

    def _evaluate(self, key, receipt):
        evaluation_key = "evaluation." + key
        reference = self.policy.feedback.evaluator
        from .upstream import evaluator_identity
        self.store.immutable("evaluator.json", evaluator_identity(reference))
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
            from .upstream import normalize_development_feedback
            self._usage(result.get("usage", ()))
            normalize_development_feedback(result, self.policy.feedback)
            self.store.finish(evaluation_key, result)
        self.evaluations[key] = result
        self._usage(result.get("usage", ()))

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

    def _capture_models(self, project_root, application_root):
        from agentloom.config.config import load_project_config

        with self._private_bindings():
            config = load_project_config(project_root)
            catalog = config.llm.overlay_from_yaml(application_root / "config/model.yaml")
            catalog = catalog.patch_from_yaml(application_root / "config/model.local.yaml")
        self.model_config = catalog.model_dump(mode="json")

    def prepare(self):
        from .isolation import preflight_isolation
        from .snapshot import load_bundle, snapshot_application

        preflight_isolation()
        for method in ("prepare", "execute_once", "reconcile", "publish", "validate_candidate", "preflight"):
            if not callable(getattr(self.driver, method, None)):
                raise ValueError(f"Application driver lacks required {method} capability")
        if self.policy.feedback.mode == "development_score":
            load_driver(self.policy.feedback.evaluator)
        from .upstream import evaluator_identity, method_identity
        self.store.immutable("method.json", method_identity())
        self.store.immutable("evaluator.json", evaluator_identity(self.policy.feedback.evaluator))
        self.store.immutable("request.json", self.request)
        self.store.immutable("policy.json", _policy_dict(self.policy))
        base_record = self.store.read("base.json")
        if base_record:
            self.base = load_bundle(Path(base_record["root"]))
            self.base.verify()
            # Original preflight may inspect live source/business inputs.
            # Recovery validates archived configuration via _validate below;
            # only private capability references are rebound from live files.
            self._restore_credentials()
            if self.model_config is None:
                self._capture_models(self.base.project_root, self.base.application_root)
            self._freeze_models()
        else:
            self.driver.preflight(self.request, self.policy)
            if self.model_config is None:
                self._capture_models(self.request.project_root, self.request.application_root)
            self._freeze_models()
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
        from .roles import preflight_roles

        with self._private_bindings():
            preflight_roles(self.base, self.policy)
        # Business memory is a frozen host input. Experiment experience is the
        # upstream Library, supplied only to improver workspaces.
        view = self.store.read("memory-view.json")
        if view is None:
            snapshot = getattr(self.driver, "memory_snapshot", lambda invocation: "")(self.invocation)
            if not isinstance(snapshot, str):
                raise ValueError("Driver memory snapshot must be fixed text")
            view = self.store.immutable("memory-view.json", {"memory_snapshot": snapshot})
        self.memory_snapshot = view["memory_snapshot"]

    def run(self):
        from .upstream import evaluator_identity, method_identity
        old_method = self.store.read("method.json")
        if self.store.read("request.json") is not None and old_method != method_identity():
            raise ValueError("Incompatible Harness method archive; create a new experiment")
        if self.store.read("request.json") is not None and self.store.read("evaluator.json") != evaluator_identity(self.policy.feedback.evaluator):
            raise ValueError("Development evaluator identity changed; create a new experiment")
        old = self.store.read("result.json")
        if old and old["status"] == "completed":
            from .snapshot import load_bundle

            self.store.immutable("request.json", self.request)
            self.store.immutable("policy.json", _policy_dict(self.policy))
            load_bundle(Path(self.store.read("base.json")["root"]))
            invocation = PreparedInvocation.from_dict(self.store.read("prepared.json"))
            _verify_inputs(invocation, self.store.root / "inputs")
            if old["task_snapshot_id"] != invocation.task_snapshot_id:
                raise ValueError("Completed result belongs to another frozen task")
            selected = self.store.root / "bundles" / old["selected_candidate_id"].split(":", 1)[1]
            if load_bundle(selected).candidate_id != old["selected_candidate_id"]:
                raise ValueError("Completed result candidate identity changed")
            from .upstream import artifacts_intact
            receipt = next((ExecutionReceipt.from_dict(item) for item in old["trials"]
                            if item["trial_id"] == old["selected_trial_id"]), None)
            if receipt is None or not receipt.eligible or not artifacts_intact(receipt):
                raise ValueError("Completed result selected artifacts are missing or changed")
            if json_value(receipt.output) != old["output"]:
                raise ValueError("Completed result output differs from its selected receipt")
            return ApplicationExecutionResult.from_dict(old)
        self.prepare()
        from agentloom.runtimes.pi.credentials import bind_pi_credentials, capture_pi_credentials

        with bind_pi_credentials(capture_pi_credentials(self.model_config)):
            return self._run_prepared()

    def _run_prepared(self):
        from .upstream import run_method

        selected, delivery, reason, library_result, method_report = None, {}, "", {}, {}
        status = "failed"
        try:
            self._check_stop()
            method = run_method(self)
            method_report = method.to_dict()
            self.errors.extend(method.errors)
            selection = method.selections.get(method.mode, {})
            if not selection.get("ok"):
                raise ValueError("Upstream selection failed: " + str(selection.get("error", "no eligible final")))
            label = selection.get("chosen_label", "")
            selected = self.receipts.get(label)
            if selected is None or not selected.eligible:
                raise ValueError("Upstream selected a missing, unaccepted or uncleaned Trial")
            reason = str(selection.get("why", ""))
            self._check_stop()
            decision_id = fingerprint([self.experiment_id, selected.trial_id, selected.candidate_id])
            publication = self.store.begin("publication", {"selected": selected, "decision_id": decision_id})
            if publication["status"] == "completed":
                delivery = publication["result"]
            else:
                delivery = json_value(self.driver.publish(self.invocation, selected, decision_id))
                self.store.finish("publication", delivery)
            library_result = method.library_result
            status = "completed"
        except EvaluationPending as error:
            self.errors.append(str(error))
            status = "partial"
        except (KeyboardInterrupt, ExperimentStopped) as error:
            self.errors.append(str(error) or "Application experiment stopped")
            status = "stopped"
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
            status = "partial" if delivery else "failed"
        result = ApplicationExecutionResult(
            self.experiment_id, self.invocation.task_snapshot_id, status,
            selected.output if selected else None,
            selected.trial_id if selected else "", selected.candidate_id if selected else "",
            reason, delivery,
            tuple(sorted(self.trials, key=lambda receipt: receipt.trial_id)),
            tuple(sorted(self.role_receipts, key=lambda receipt: receipt["stage"])),
            self._usage(()), tuple(self.errors), library_result, method_report,
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
    experiment_id=None,
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
    if resume_id is not None and experiment_id is not None:
        raise ValueError("Provide either a new experiment identity or a resume identity")
    if resume_id is not None:
        experiment_id = resume_id
    elif experiment_id is None:
        experiment_id = "experiment-" + uuid.uuid4().hex
    if not isinstance(experiment_id, str) or not experiment_id or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in experiment_id
    ):
        raise ValueError("Invalid experiment resume identity")
    if resume_id is not None and policy is None:
        # Enabling/disabling or corrupting live Application YAML cannot
        # redefine an already frozen experiment's policy on recovery.
        from .policy import HarnessingPolicy
        if state_root is None:
            from agentloom.config.config import load_project_config
            from agentloom.execution.context import resolve_runtime_home
            config = load_project_config(request.project_root)
            state_root = resolve_runtime_home(config.raw, agent_root=request.project_root).root_dir / "harnessing"
        resumed = ExperimentStore(Path(state_root) / experiment_id)
        try:
            stored_policy = resumed.read("policy.json")
        finally:
            resumed.close()
        if stored_policy is None:
            raise ValueError("No frozen experiment policy exists for this resume identity")
        policy = HarnessingPolicy.model_validate(stored_policy)
    if policy is None:
        from agentloom.app.application import _system_settings
        from agentloom.config.config import load_project_config

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
    if state_root is None:
        from agentloom.config.config import load_project_config
        from agentloom.execution.context import resolve_runtime_home

        config = load_project_config(request.project_root)
        state_root = resolve_runtime_home(config.raw, agent_root=request.project_root).root_dir / "harnessing"
    store = ExperimentStore(Path(state_root) / experiment_id)
    try:
        with store.lock():
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
