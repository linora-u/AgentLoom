"""Direct tests for the real model-protocol matrix orchestrator."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "model_protocol_matrix",
    Path(__file__).parent / "acceptance/model_protocol_matrix.py",
)
matrix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = matrix
SPEC.loader.exec_module(matrix)


def _settings(
    adapter: str,
    model: str,
    *,
    api_key: str = "",
    base_url: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        adapter=adapter,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def test_select_cases_uses_one_explicit_profile_per_adapter() -> None:
    config = SimpleNamespace(
        default_model_type="chat_default",
        models={
            "summary": _settings(
                "openai_responses",
                "openai/summary-response",
                api_key="response-key",
            ),
            "responses_primary": _settings(
                "openai_responses",
                "openai/response",
                api_key="response-key",
            ),
            "chat_other": _settings(
                "openai_chat",
                "openai/chat-other",
                api_key="chat-key",
            ),
            "chat_default": _settings(
                "openai_chat",
                "openai/chat-default",
                api_key="chat-key",
            ),
            "claude": _settings(
                "anthropic_messages",
                "anthropic/claude",
                api_key="claude-key",
            ),
        },
    )

    cases = matrix.select_cases(config, environ={})

    assert [(case.adapter, case.profile, case.runnable) for case in cases] == [
        ("openai_chat", "chat_default", True),
        ("openai_responses", "responses_primary", True),
        ("anthropic_messages", "claude", True),
    ]


def test_select_cases_reports_missing_profile_or_credentials_as_not_run() -> None:
    config = SimpleNamespace(
        default_model_type="chat",
        models={
            "chat": _settings(
                "openai_chat",
                "openai/chat",
                api_key="",
                base_url="https://provider.example/v1",
            ),
        },
    )

    cases = matrix.select_cases(config, environ={})

    assert cases[0].profile == "chat"
    assert cases[0].runnable is False
    assert "credential" in cases[0].reason
    assert cases[1].profile is None
    assert cases[1].runnable is False
    assert "no explicit" in cases[1].reason
    assert cases[2].profile is None


def test_redaction_removes_configured_secrets_and_urls() -> None:
    secret = "sk-private-123456789"
    text = (
        f"api_key={secret} base_url=https://provider.example/v1 "
        f"Authorization: Bearer {secret}"
    )

    redacted = matrix.redact_text(text, secrets=(secret,))

    assert secret not in redacted
    assert "provider.example" not in redacted
    assert "<redacted" in redacted


def test_redaction_removes_common_secret_shapes_without_configured_values() -> None:
    secret = "sk-unlisted-secret-123456"
    text = (
        f"api_key={secret} Authorization: Bearer {secret} "
        "https://provider.example/v1"
    )

    redacted = matrix.redact_text(text)

    assert secret not in redacted
    assert "provider.example" not in redacted
    assert "api_key=<redacted>" in redacted
    assert "Authorization=<redacted>" in redacted


def test_provider_environment_credentials_do_not_cross_adapter_boundaries() -> None:
    config = SimpleNamespace(
        default_model_type="chat",
        models={
            "chat": _settings("openai_chat", "openai/chat"),
            "responses": _settings("openai_responses", "openai/response"),
            "claude": _settings(
                "anthropic_messages",
                "anthropic/claude",
            ),
        },
    )

    cases = matrix.select_cases(
        config,
        environ={"OPENAI_API_KEY": "openai-only-secret"},
    )

    assert cases[0].runnable is True
    assert cases[1].runnable is True
    assert cases[2].runnable is False
    assert "credential" in cases[2].reason


def test_application_workflow_path_is_unique_and_discoverable(tmp_path) -> None:
    first = matrix.application_workflow_path(tmp_path / "one", "openai_chat")
    second = matrix.application_workflow_path(tmp_path / "two", "openai_chat")

    assert first != second
    assert first.parent.name == "workflows"
    assert first.is_relative_to(matrix.ROOT / "applications")
    assert first.parents[1].name.startswith(
        "architecture_acceptance_protocol_openai_chat_"
    )


def test_checkpoint_path_uses_explicit_runtime_root_for_nested_application(
    tmp_path,
) -> None:
    run = SimpleNamespace(
        application_id="nested/protocol-case",
        task_id="task-1",
        run_id="run-1",
        manifest_path=tmp_path / "unrelated/manifest.json",
    )

    path = matrix._checkpoint_path(run, runtime_root=tmp_path / "runtime")

    assert path == (
        tmp_path
        / "runtime/checkpoints/nested/protocol-case/task-1/checkpoint.json"
    )


def _checkpoint_fixture(
    *,
    include_call: bool = True,
    runtime_version: str = "1.26.0",
) -> dict:
    call_id = "call-echo"
    canonical = []
    if include_call:
        canonical.extend(
            [
                {
                    "step_index": 0,
                    "item_index": 0,
                    "response_id": "response-1",
                    "item": {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "protocol_matrix_echo",
                        "arguments_json": (
                            '{"token":"AGENTLOOM_PROTOCOL_MATRIX_OK"}'
                        ),
                        "item_id": "item-echo",
                    },
                },
                {
                    "step_index": 0,
                    "item_index": 1,
                    "response_id": "response-1",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": matrix.TOKEN,
                        "item_id": None,
                        "status": "completed",
                        "is_error": False,
                        "replay_payload": {},
                    },
                },
            ]
        )
    canonical.extend(
        [
            {
                "step_index": 0,
                "item_index": len(canonical),
                "response_id": "response-1",
                "item": {
                    "type": "function_call",
                    "call_id": "call-final",
                    "name": "final_answer",
                    "arguments_json": (
                        '{"answer":"AGENTLOOM_PROTOCOL_MATRIX_OK"}'
                    ),
                    "item_id": "item-final",
                },
            },
            {
                "step_index": 0,
                "item_index": len(canonical) + 1,
                "response_id": "response-1",
                "item": {
                    "type": "function_call_output",
                    "call_id": "call-final",
                    "output": matrix.TOKEN,
                    "item_id": None,
                    "status": "completed",
                    "is_error": False,
                    "replay_payload": {},
                },
            },
        ]
    )
    return {
        "runtime_checkpoint": {
            "runtime_id": "smolagents",
            "runtime_version": runtime_version,
            "state_schema_version": 2,
            "task_id": "task-1",
            "run_id": "run-1",
            "progress": 1,
            "audit_metadata": {},
            "payload": {
                "memory_steps": [
                    {
                        "_step_type": "ActionStep",
                        "tool_results": [
                            {
                                "call_id": "call-echo",
                                "tool_name": "protocol_matrix_echo",
                                "input": {"token": matrix.TOKEN},
                                "status": "completed",
                                "output": matrix.TOKEN,
                                "error": None,
                                "stage": "completed",
                            },
                            {
                                "call_id": "call-final",
                                "tool_name": "final_answer",
                                "input": {"answer": matrix.TOKEN},
                                "status": "completed",
                                "output": matrix.TOKEN,
                                "error": None,
                                "stage": "completed",
                            },
                        ],
                    }
                ],
                "canonical_model_items": canonical,
            },
        }
    }


def test_checkpoint_evidence_requires_correlated_canonical_calls(
    tmp_path,
) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(_checkpoint_fixture()), encoding="utf-8")

    result = matrix._validate_checkpoint(
        path,
        task_id="task-1",
        run_id="run-1",
    )

    assert result["runtime_version"] == "1.26.0"
    assert result["canonical_item_count"] == 4
    assert {
        record["tool_name"] for record in result["tool_records"]
    } == {"protocol_matrix_echo", "final_answer"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_checkpoint_fixture(include_call=False), "correlated"),
        (_checkpoint_fixture(runtime_version=""), "runtime_version"),
    ],
)
def test_checkpoint_evidence_rejects_incomplete_runtime_contract(
    tmp_path,
    payload,
    message,
) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AssertionError, match=message):
        matrix._validate_checkpoint(
            path,
            task_id="task-1",
            run_id="run-1",
        )


def test_execute_matrix_continues_after_failure_and_preserves_not_run(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(matrix, "_revision", lambda: "revision")
    cases = (
        matrix.MatrixCase(
            adapter="openai_chat",
            profile="chat",
            model="openai/chat",
            runnable=True,
        ),
        matrix.MatrixCase(
            adapter="openai_responses",
            profile=None,
            model=None,
            runnable=False,
            reason="missing profile",
        ),
        matrix.MatrixCase(
            adapter="anthropic_messages",
            profile="claude",
            model="anthropic/claude",
            runnable=True,
        ),
    )
    calls: list[str] = []

    def run(case, workspace, **_kwargs):
        calls.append(case.adapter)
        if case.adapter == "openai_chat":
            raise RuntimeError("api_key=secret https://provider.example/v1 failed")
        return {
            "revision": "revision",
            "adapter": case.adapter,
            "profile": case.profile,
            "model": case.model,
            "status": "PASSED",
        }

    reports = matrix.execute_matrix(
        cases,
        tmp_path,
        selected=matrix.ADAPTERS,
        timeout_seconds=1,
        secrets=("secret",),
        runner=run,
    )

    assert calls == ["openai_chat", "anthropic_messages"]
    assert [report["status"] for report in reports] == [
        "FAILED",
        "NOT-RUN",
        "PASSED",
    ]
    assert "secret" not in reports[0]["reason"]
    assert "provider.example" not in reports[0]["reason"]
    assert matrix.matrix_exit_code(reports) == 1
    assert json.loads((tmp_path / "summary.json").read_text()) == reports


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["PASSED", "NOT-RUN"], 0),
        (["NOT-RUN", "NOT-RUN"], 0),
        (["PASSED", "FAILED", "NOT-RUN"], 1),
    ],
)
def test_matrix_exit_code_fails_only_for_failed_runnable_case(
    statuses,
    expected,
) -> None:
    assert matrix.matrix_exit_code(
        [{"status": status} for status in statuses]
    ) == expected
