"""Private Studio method dispatcher owned by the Studio package."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from agentloom.application.studio.errors import StudioServiceError
from agentloom.application.studio.query_service import StudioQueryService


class StudioAdapterError(RuntimeError):
    """A stable, user-safe RPC error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScheduleMutations(Protocol):
    def add(
        self,
        *,
        yaml_path: str,
        name: str,
        schedule: Any,
    ) -> dict[str, Any]: ...

    def mutate(self, action: str, *, job_id: str) -> dict[str, Any]: ...


class StudioDispatcher:
    """Validate Studio requests and dispatch them to their business owners."""

    def __init__(
        self,
        project_root: Path,
        *,
        builder_service: Any | None = None,
        schedule_mutations: ScheduleMutations | None = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self._queries = StudioQueryService(self.project_root)
        self._builder = builder_service
        self._schedule_mutations = schedule_mutations

    def _builder_service(self) -> Any:
        if self._builder is None:
            from agentloom.application.studio.builder import BuilderService

            self._builder = BuilderService(self.project_root)
        return self._builder

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch(method, params, event_sink=None)

    def dispatch_with_events(
        self,
        method: str,
        params: dict[str, Any],
        event_sink: Any,
    ) -> dict[str, Any]:
        return self._dispatch(method, params, event_sink=event_sink)

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        *,
        event_sink: Any | None,
    ) -> dict[str, Any]:
        try:
            if method == "bootstrap":
                self._exact_params(params, set(), method=method)
                return self._queries.bootstrap()
            if method == "runtime.summary":
                self._exact_params(params, set(), method=method)
                return self._queries.runtime_summary()
            if method == "system.detail":
                system_id = self._required_wire_string(
                    params.get("system_id"),
                    field="system_id",
                )
                self._exact_params(params, {"system_id"}, method=method)
                return self._queries.system_detail(system_id)
            if method == "application.detail":
                self._exact_params(params, {"application_id"}, method=method)
                return self._queries.application_detail(
                    self._required_wire_string(
                        params["application_id"],
                        field="application_id",
                    )
                )
            if method == "run.detail":
                return self._run_detail(params)
            if method == "schedule.add":
                return self._schedule_add(params)
            if method in {
                "schedule.pause",
                "schedule.resume",
                "schedule.remove",
            }:
                return self._schedule_mutation(method, params)
            if method in {"assistant.send", "builder.send"}:
                return self._builder_send(params, event_sink=event_sink)
            if method == "builder.draft":
                self._exact_params(params, {"session_id"}, method=method)
                return self._builder_service().get_draft(
                    self._required_wire_string(
                        params["session_id"],
                        field="session_id",
                    )
                )
            if method == "draft.apply":
                return self._draft_apply(params)
        except StudioServiceError as error:
            raise StudioAdapterError(error.code, str(error)) from error
        raise StudioAdapterError("method_not_found", f"unknown method: {method}")

    def _run_detail(self, params: dict[str, Any]) -> dict[str, Any]:
        expected = {"run_id", "application_id"}
        if "system_id" in params:
            expected.add("system_id")
        self._exact_params(params, expected, method="run.detail")
        system_id = params.get("system_id")
        if system_id is not None:
            system_id = self._required_wire_string(
                system_id,
                field="system_id",
            )
        return self._queries.run_detail(
            self._required_wire_string(params["run_id"], field="run_id"),
            application_id=self._required_wire_string(
                params["application_id"],
                field="application_id",
            ),
            system_id=system_id,
        )

    def _schedule_add(self, params: dict[str, Any]) -> dict[str, Any]:
        from agentloom.schedules.store import (
            JobBusyError,
            JobNotFoundError,
            ScheduleStoreError,
        )

        self._exact_params(
            params,
            {"yaml_path", "name", "schedule"},
            method="schedule.add",
        )
        try:
            yaml_path = self._required_wire_string(
                params["yaml_path"],
                field="yaml_path",
            )
            job = self._schedule_mutation_service().add(
                yaml_path=yaml_path,
                name=params["name"],
                schedule=params["schedule"],
            )
            return self._schedule_result("add", job)
        except ValueError as error:
            raise StudioAdapterError("invalid_params", str(error)) from error
        except JobNotFoundError as error:
            raise StudioAdapterError("not_found", str(error)) from error
        except JobBusyError as error:
            raise StudioAdapterError("busy", str(error)) from error
        except ScheduleStoreError as error:
            raise StudioAdapterError("schedule_failed", str(error)) from error

    def _schedule_mutation(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        from agentloom.schedules.store import (
            JobBusyError,
            JobNotFoundError,
            ScheduleStoreError,
        )

        self._exact_params(params, {"job_id"}, method=method)
        try:
            action = method.removeprefix("schedule.")
            job = self._schedule_mutation_service().mutate(
                action,
                job_id=self._required_wire_string(
                    params["job_id"],
                    field="job_id",
                ),
            )
            return self._schedule_result(action, job)
        except ValueError as error:
            raise StudioAdapterError("invalid_params", str(error)) from error
        except JobNotFoundError as error:
            raise StudioAdapterError("not_found", str(error)) from error
        except JobBusyError as error:
            raise StudioAdapterError("busy", str(error)) from error
        except ScheduleStoreError as error:
            raise StudioAdapterError("schedule_failed", str(error)) from error

    def _schedule_mutation_service(self) -> ScheduleMutations:
        if self._schedule_mutations is None:
            raise StudioAdapterError(
                "internal_error",
                "Schedule mutations are unavailable",
            )
        return self._schedule_mutations

    def _builder_send(
        self,
        params: dict[str, Any],
        *,
        event_sink: Any | None,
    ) -> dict[str, Any]:
        unexpected = set(params) - {"session_id", "message", "model_type"}
        missing = {"session_id", "message"} - set(params)
        if missing or unexpected:
            expected = {"session_id", "message"}
            if "model_type" in params:
                expected.add("model_type")
            self._exact_params(params, expected, method="builder.send")
        session_id = self._required_wire_string(
            params["session_id"],
            field="session_id",
        )
        message = self._required_wire_string(
            params["message"],
            field="message",
        )
        model_type = params.get("model_type")
        if model_type is not None:
            model_type = self._required_wire_string(
                model_type,
                field="model_type",
            )
        try:
            send_params: dict[str, Any] = {
                "session_id": session_id,
                "message": message,
                "model_type": model_type,
            }
            if event_sink is not None:
                send_params["on_event"] = event_sink
            return self._builder_service().send(**send_params)
        except Exception as error:
            from agentloom.application.studio.chat_agent import ChatAgentError

            if isinstance(error, ChatAgentError):
                raise StudioAdapterError(error.code, str(error)) from error
            if isinstance(error, ValueError):
                raise StudioAdapterError("builder_failed", str(error)) from error
            raise StudioAdapterError(
                "builder_failed",
                "Builder model call failed; retry or select another configured model.",
            ) from error

    def _draft_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        from agentloom.application.studio.builder import DraftConflictError

        self._exact_params(
            params,
            {"session_id", "expected_revision"},
            method="draft.apply",
        )
        session_id = self._required_wire_string(
            params["session_id"],
            field="session_id",
        )
        expected_revision = params["expected_revision"]
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision,
            int,
        ):
            raise StudioAdapterError(
                "invalid_params",
                "expected_revision must be an integer",
            )
        try:
            return self._builder_service().apply_draft(
                session_id=session_id,
                expected_revision=expected_revision,
            )
        except DraftConflictError as error:
            raise StudioAdapterError("draft_conflict", str(error)) from error
        except ValueError as error:
            raise StudioAdapterError("builder_failed", str(error)) from error
        except RuntimeError as error:
            message = str(error)
            if message.startswith("Agent draft apply failed and rollback was incomplete:"):
                raise StudioAdapterError("builder_failed", message) from error
            raise StudioAdapterError(
                "builder_failed",
                "Agent draft apply failed; no recovery details are available.",
            ) from error

    @staticmethod
    def _schedule_result(
        action: str,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "action": action,
            "job_id": str(job["id"]),
            "name": str(job["name"]),
            "state": str(job["state"]),
        }

    @staticmethod
    def _exact_params(
        params: dict[str, Any],
        expected: set[str],
        *,
        method: str,
    ) -> None:
        actual = set(params)
        if actual == expected:
            return
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise StudioAdapterError(
            "invalid_params",
            f"{method} params are invalid ({'; '.join(details)})",
        )

    @staticmethod
    def _required_wire_string(value: Any, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise StudioAdapterError(
                "invalid_params",
                f"{field} must be a non-empty string",
            )
        return value
