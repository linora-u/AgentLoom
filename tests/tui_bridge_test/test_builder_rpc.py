from __future__ import annotations

from pathlib import Path

import pytest

from src.tui_bridge.bridge import BridgeError, TuiBridge
from src.tui_bridge.builder import DraftConflictError


class _FakeBuilder:
    def __init__(self) -> None:
        self.calls = []

    def send(self, **params):
        self.calls.append(("send", params))
        return {
            "session_id": params["session_id"],
            "assistant": "ready",
            "model_type": params.get("model_type"),
            "draft": {"revision": 1, "valid": True, "errors": [], "files": []},
        }

    def get_draft(self, session_id: str):
        self.calls.append(("draft", {"session_id": session_id}))
        return {"revision": 1, "valid": True, "errors": [], "files": []}

    def apply_draft(self, **params):
        self.calls.append(("apply", params))
        if params["expected_revision"] != 1:
            raise DraftConflictError("Draft revision changed")
        return {"applied": True, "revision": 1, "files": ["agent.yaml"]}


def test_bridge_routes_builder_conversation_draft_and_explicit_apply(tmp_path: Path) -> None:
    builder = _FakeBuilder()
    bridge = TuiBridge(tmp_path, builder_service=builder)

    sent = bridge.dispatch(
        "builder.send",
        {"session_id": "session-1", "message": "create", "model_type": "powerful"},
    )
    draft = bridge.dispatch("builder.draft", {"session_id": "session-1"})
    applied = bridge.dispatch(
        "draft.apply",
        {"session_id": "session-1", "expected_revision": 1},
    )

    assert sent["assistant"] == "ready"
    assert draft["revision"] == 1
    assert applied["applied"] is True
    assert builder.calls == [
        (
            "send",
            {"session_id": "session-1", "message": "create", "model_type": "powerful"},
        ),
        ("draft", {"session_id": "session-1"}),
        ("apply", {"session_id": "session-1", "expected_revision": 1}),
    ]


def test_bridge_rejects_stale_apply_as_a_structured_conflict(tmp_path: Path) -> None:
    bridge = TuiBridge(tmp_path, builder_service=_FakeBuilder())

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "draft.apply",
            {"session_id": "session-1", "expected_revision": 0},
        )

    assert error.value.code == "draft_conflict"
    assert "revision" in str(error.value)


def test_bridge_preserves_recovery_instructions_for_incomplete_rollback(tmp_path: Path) -> None:
    class _RecoveryBuilder(_FakeBuilder):
        def apply_draft(self, **_params):
            raise RuntimeError(
                "Agent draft apply failed and rollback was incomplete: "
                "/project/agent.yaml; recovery backup preserved at /project/.agent.yaml.bak"
            )

    bridge = TuiBridge(tmp_path, builder_service=_RecoveryBuilder())

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "draft.apply",
            {"session_id": "session-1", "expected_revision": 1},
        )

    assert error.value.code == "builder_failed"
    assert "recovery backup preserved at /project/.agent.yaml.bak" in str(error.value)


def test_bridge_turns_model_failures_into_a_safe_builder_error(tmp_path: Path) -> None:
    class _FailedBuilder(_FakeBuilder):
        def send(self, **_params):
            raise RuntimeError("provider response included secret diagnostics")

    bridge = TuiBridge(tmp_path, builder_service=_FailedBuilder())

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "builder.send",
            {"session_id": "session-1", "message": "create"},
        )

    assert error.value.code == "builder_failed"
    assert "retry" in str(error.value).lower()
    assert "secret diagnostics" not in str(error.value)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("builder.send", {"session_id": "", "message": "create"}),
        ("builder.send", {"session_id": "session-1", "message": ""}),
        ("builder.draft", {"session_id": 3}),
        ("draft.apply", {"session_id": "session-1", "expected_revision": True}),
    ],
)
def test_builder_rpc_validates_wire_params(method: str, params: dict, tmp_path: Path) -> None:
    bridge = TuiBridge(tmp_path, builder_service=_FakeBuilder())

    with pytest.raises(BridgeError) as error:
        bridge.dispatch(method, params)

    assert error.value.code == "invalid_params"
