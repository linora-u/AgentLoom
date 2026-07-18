from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.lib.smolagents.hooks import (
    HookConfigLayer,
    HookEvent,
    HookPlanCompiler,
)


def _layer(
    root: Path,
    name: str,
    priority: int,
    hooks: dict | None,
) -> HookConfigLayer:
    config = {} if hooks is None else {"hooks": hooks}
    return HookConfigLayer(
        name=name,
        config=config,
        agent_root=root,
        source_path=root / f"{name}.yaml",
        priority=priority,
    )


def _direct(hook_id: str, command: str, **extra: object) -> dict[str, object]:
    return {"id": hook_id, "command": command, **extra}


def _write_bundle(
    root: Path,
    directory: str,
    manifest: str,
) -> Path:
    bundle = root / directory
    bundle.mkdir(parents=True)
    (bundle / "HOOK.yaml").write_text(manifest, encoding="utf-8")
    return bundle


def test_layer_takes_an_immutable_snapshot(tmp_path: Path) -> None:
    raw = {"hooks": {"PreToolUse": [_direct("one", "true")]}}
    layer = HookConfigLayer("global", raw, tmp_path, tmp_path / "system.yaml", 0)

    raw["hooks"]["PreToolUse"][0]["command"] = "false"

    assert layer.config["hooks"]["PreToolUse"][0]["command"] == "true"
    with pytest.raises(TypeError):
        layer.config["hooks"]["PreToolUse"] = ()
    with pytest.raises(FrozenInstanceError):
        layer.name = "changed"  # type: ignore[misc]


def test_compile_orders_bundles_before_direct_entries_and_keeps_provenance(
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(
        tmp_path,
        "hooks/demo",
        """
name: demo
description: Demo hooks.
hooks:
  TaskCreated:
    - id: demo.task
      command: python scripts/task.py
      timeout: 4
  PreToolUse:
    - id: demo.pre
      matcher: write_file|edit_file
      command: python scripts/pre.py
""",
    )
    layer = _layer(
        tmp_path,
        "global",
        0,
        {
            "bundles": {"demo": {"path": "hooks/demo"}},
            "PreToolUse": [_direct("direct.pre", "python direct.py")],
            "SessionStart": [_direct("direct.session", "python session.py")],
        },
    )

    plan = HookPlanCompiler().compile([layer])

    assert [handler.hook_id for handler in plan.handlers] == [
        "demo.task",
        "demo.pre",
        "direct.pre",
        "direct.session",
    ]
    assert [handler.event for handler in plan.handlers] == [
        HookEvent.TASK_CREATED,
        HookEvent.PRE_TOOL_USE,
        HookEvent.PRE_TOOL_USE,
        HookEvent.SESSION_START,
    ]
    assert plan.handlers[0].cwd == str(bundle.resolve())
    assert plan.handlers[0].source_path == str((bundle / "HOOK.yaml").resolve())
    assert plan.handlers[2].cwd == str(tmp_path.resolve())
    assert plan.handlers[2].source_path == str((tmp_path / "global.yaml").resolve())
    assert plan.handlers[2].shell_spec.timeout == 20
    assert len(plan.fingerprint) == 64

    assert [handler.hook_id for handler in plan.matching(HookEvent.PRE_TOOL_USE, "write_file")] == [
        "demo.pre",
        "direct.pre",
    ]
    assert [handler.hook_id for handler in plan.matching(HookEvent.PRE_TOOL_USE, "write_file_extra")] == [
        "direct.pre"
    ]


def test_bundle_is_not_discovered_without_an_explicit_reference(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        "hooks/hidden",
        """
name: hidden
hooks:
  SessionStart:
    - id: hidden.start
      command: true
""",
    )

    plan = HookPlanCompiler().compile([_layer(tmp_path, "global", 0, {})])

    assert plan.handlers == ()


@pytest.mark.parametrize("invalid", [None, [], "PreToolUse"])
def test_present_hooks_value_must_be_a_mapping(
    tmp_path: Path,
    invalid: object,
) -> None:
    layer = HookConfigLayer(
        name="agent",
        config={"hooks": invalid},
        agent_root=tmp_path,
        source_path=tmp_path / "agent.yaml",
        priority=0,
    )

    with pytest.raises(ValueError, match="Top-level hooks.*must be a mapping"):
        HookPlanCompiler().compile([layer])


def test_higher_layer_same_event_id_fully_replaces_and_moves_entry(
    tmp_path: Path,
) -> None:
    low = _layer(
        tmp_path,
        "global",
        0,
        {"PreToolUse": [_direct("shared", "low", timeout=1), _direct("keep", "keep")]},
    )
    high = _layer(
        tmp_path,
        "agent",
        20,
        {"PreToolUse": [_direct("shared", "high", matcher="write_file", timeout=9)]},
    )

    plan = HookPlanCompiler().compile([high, low])

    assert [handler.hook_id for handler in plan.handlers] == ["keep", "shared"]
    shared = plan.handlers[-1].shell_spec
    assert shared is not None
    assert shared.command == "high"
    assert shared.timeout == 9
    assert shared.matcher == "write_file"
    assert shared.layer_name == "agent"


def test_direct_tombstone_removes_inherited_entry(tmp_path: Path) -> None:
    low = _layer(
        tmp_path,
        "global",
        0,
        {"Stop": [_direct("stop.guard", "guard")]},
    )
    high = _layer(
        tmp_path,
        "agent",
        20,
        {"Stop": [{"id": "stop.guard", "enabled": False}]},
    )

    plan = HookPlanCompiler().compile([low, high])

    assert plan.handlers == ()


def test_bundle_replacement_removes_old_bundle_entries(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        "hooks/old",
        """
name: visual
hooks:
  SessionStart:
    - id: visual.old-a
      command: old-a
    - id: visual.old-b
      command: old-b
""",
    )
    _write_bundle(
        tmp_path,
        "hooks/new",
        """
name: visual
hooks:
  SessionEnd:
    - id: visual.new
      command: new
""",
    )
    low = _layer(
        tmp_path,
        "global",
        0,
        {"bundles": {"visual": {"path": "hooks/old"}}},
    )
    high = _layer(
        tmp_path,
        "agent",
        20,
        {"bundles": {"visual": {"path": "hooks/new"}}},
    )

    plan = HookPlanCompiler().compile([low, high])

    assert [handler.hook_id for handler in plan.handlers] == ["visual.new"]


def test_bundle_tombstone_only_removes_entries_still_owned_by_bundle(
    tmp_path: Path,
) -> None:
    _write_bundle(
        tmp_path,
        "hooks/visual",
        """
name: visual
hooks:
  SessionStart:
    - id: visual.start
      command: bundled
  SessionEnd:
    - id: visual.end
      command: bundled
""",
    )
    low = _layer(
        tmp_path,
        "global",
        0,
        {"bundles": {"visual": {"path": "hooks/visual"}}},
    )
    middle = _layer(
        tmp_path,
        "application",
        10,
        {"SessionStart": [_direct("visual.start", "direct-override")]},
    )
    high = _layer(
        tmp_path,
        "agent",
        20,
        {"bundles": {"visual": {"enabled": False}}},
    )

    plan = HookPlanCompiler().compile([low, middle, high])

    assert [handler.hook_id for handler in plan.handlers] == ["visual.start"]
    assert plan.handlers[0].shell_spec.command == "direct-override"


@pytest.mark.parametrize(
    ("hooks", "message"),
    [
        (
            {"PreToolUse": [_direct("duplicate", "one"), _direct("duplicate", "two")]},
            "duplicate Hook id 'duplicate'",
        ),
        (
            {
                "PreToolUse": [_direct("cross-event", "one")],
                "PostToolUse": [_direct("cross-event", "two")],
            },
            "used for both PreToolUse and PostToolUse",
        ),
        ({"UnknownEvent": []}, "Unknown hook event"),
        ({"SessionStart": [_direct("matcher", "true", matcher="tool")]}, "matcher is only valid"),
        ({"SessionStart": [_direct("star-matcher", "true", matcher="*")]}, "matcher is only valid"),
        ({"PreToolUse": [_direct("bad-regex", "true", matcher="[")]}, "invalid matcher"),
        ({"PreToolUse": [_direct("extra", "true", once=True)]}, "unsupported field"),
        ({"PreToolUse": [_direct("timeout", "true", timeout=0)]}, "positive"),
        ({"PreToolUse": [{"id": "missing"}]}, "requires a non-empty command"),
        ({"PreToolUse": [{"id": "bad-disable", "enabled": False, "command": "true"}]}, "tombstone"),
    ],
)
def test_invalid_direct_config_is_rejected(
    tmp_path: Path,
    hooks: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        HookPlanCompiler().compile([_layer(tmp_path, "global", 0, hooks)])


def test_cross_layer_cross_event_id_is_rejected(tmp_path: Path) -> None:
    low = _layer(tmp_path, "global", 0, {"PreToolUse": [_direct("same", "one")]})
    high = _layer(tmp_path, "agent", 20, {"Stop": [_direct("same", "two")]})

    with pytest.raises(ValueError, match="used for both PreToolUse and Stop"):
        HookPlanCompiler().compile([low, high])


@pytest.mark.parametrize(
    ("bundle_config", "manifest", "message"),
    [
        ({"wrong": {"path": "hooks/demo"}}, "name: demo\nhooks: {}\n", "does not match"),
        (
            {"demo": {"path": "hooks/demo"}},
            "name: demo\nhooks:\n  bundles: {}\n",
            "cannot declare bundles",
        ),
        (
            {"demo": {"path": "hooks/demo", "extra": True}},
            "name: demo\nhooks: {}\n",
            "unsupported field",
        ),
    ],
)
def test_invalid_bundle_is_rejected(
    tmp_path: Path,
    bundle_config: dict,
    manifest: str,
    message: str,
) -> None:
    _write_bundle(tmp_path, "hooks/demo", manifest)

    with pytest.raises(ValueError, match=message):
        HookPlanCompiler().compile(
            [_layer(tmp_path, "global", 0, {"bundles": bundle_config})]
        )


def test_missing_bundle_manifest_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "hooks/demo").mkdir(parents=True)

    with pytest.raises(ValueError, match="HOOK.yaml"):
        HookPlanCompiler().compile(
            [
                _layer(
                    tmp_path,
                    "global",
                    0,
                    {"bundles": {"demo": {"path": "hooks/demo"}}},
                )
            ]
        )


def test_fingerprint_is_stable_and_changes_with_effective_plan(tmp_path: Path) -> None:
    first = _layer(tmp_path, "global", 0, {"Stop": [_direct("stop", "one")]})
    same = _layer(tmp_path, "global", 0, {"Stop": [_direct("stop", "one")]})
    changed = _layer(tmp_path, "global", 0, {"Stop": [_direct("stop", "two")]})

    compiler = HookPlanCompiler()
    assert compiler.compile([first]).fingerprint == compiler.compile([same]).fingerprint
    assert compiler.compile([first]).fingerprint != compiler.compile([changed]).fingerprint
