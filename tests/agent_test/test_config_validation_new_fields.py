"""Tests for RootSettings config validation with new runtime fields."""

import pytest

from src.lib.config.config import (
    _reject_application_global_only_keys,
    extract_workflow_overlay,
)
from src.lib.config.config_validation import (
    LoggingSettings,
    RootSettings,
    RuntimeSettings,
    ToolAccessControlSettings,
    validate_system_snapshot,
)

# ===========================================================================
# tool_metadata field
# ===========================================================================

class TestToolMetadataField:
    """Verify RootSettings accepts and defaults the tool_metadata field."""

    def test_tool_metadata_accepted_with_dict(self):
        """RootSettings should accept a tool_metadata dict."""
        settings = RootSettings(tool_metadata={"grep_search": {"max_results": 100}})
        assert settings.tool_metadata == {"grep_search": {"max_results": 100}}

    def test_tool_metadata_complex_nested_dict(self):
        """tool_metadata can hold deeply nested config."""
        meta = {
            "read_file": {
                "description_override": "Read file",
                "tags": ["io", "read"],
            },
            "bash_tool": {"timeout": 30},
        }
        settings = RootSettings(tool_metadata=meta)
        assert settings.tool_metadata["read_file"]["tags"] == ["io", "read"]
        assert settings.tool_metadata["bash_tool"]["timeout"] == 30

    def test_tool_metadata_empty_dict(self):
        """Explicit empty dict is valid."""
        settings = RootSettings(tool_metadata={})
        assert settings.tool_metadata == {}

    def test_tool_metadata_defaults_to_empty_dict(self):
        """When omitted, tool_metadata defaults to empty dict."""
        settings = RootSettings()
        assert settings.tool_metadata == {}


# ===========================================================================
# tool_output_limits field
# ===========================================================================

class TestToolOutputLimitsField:
    """Verify RootSettings accepts and defaults the tool_output_limits field."""

    def test_tool_output_limits_accepted_with_dict(self):
        """RootSettings should accept a tool_output_limits dict."""
        settings = RootSettings(tool_output_limits={"max_chars": 5000})
        assert settings.tool_output_limits == {"max_chars": 5000}

    def test_tool_output_limits_with_per_tool_config(self):
        """tool_output_limits can have per-tool limit overrides."""
        limits = {
            "default_max_chars": 10000,
            "bash_tool": {"max_chars": 20000},
            "grep_search": {"max_chars": 50000},
        }
        settings = RootSettings(tool_output_limits=limits)
        assert settings.tool_output_limits["default_max_chars"] == 10000
        assert settings.tool_output_limits["bash_tool"]["max_chars"] == 20000

    def test_tool_output_limits_empty_dict(self):
        """Explicit empty dict is valid."""
        settings = RootSettings(tool_output_limits={})
        assert settings.tool_output_limits == {}

    def test_tool_output_limits_defaults_to_empty_dict(self):
        """When omitted, tool_output_limits defaults to empty dict."""
        settings = RootSettings()
        assert settings.tool_output_limits == {}


# ===========================================================================
# context_engine field
# ===========================================================================

class TestContextEngineField:
    """Verify RootSettings accepts and defaults the context_engine field."""

    def test_context_engine_accepted_with_dict(self):
        settings = RootSettings(context_engine={"min_chars": 2000, "preview_max_chars": 3000})
        assert settings.context_engine["min_chars"] == 2000
        assert settings.context_engine["preview_max_chars"] == 3000

    def test_context_engine_defaults_to_empty_dict(self):
        settings = RootSettings()
        assert settings.context_engine == {}


# ===========================================================================
# Missing fields default to empty dict
# ===========================================================================

class TestMissingFieldsDefault:
    """When no fields are provided, all dict fields default to empty."""

    def test_all_dict_fields_default_empty(self):
        settings = RootSettings()
        assert settings.tool_metadata == {}
        assert settings.tool_output_limits == {}
        assert settings.model == {}
        assert settings.execution_env == {}
        assert settings.code_agent == {}
        assert settings.context_engine == {}
        assert settings.tools == []

    def test_runtime_storage_defaults_are_bounded(self):
        settings = RootSettings()
        assert settings.runtime == RuntimeSettings(root_dir=".agentloom")
        assert settings.logging == LoggingSettings()
        assert settings.logging.max_file_bytes == 25 * 1024 * 1024
        assert settings.logging.backup_count == 3

    def test_runtime_and_logging_accept_only_canonical_keys(self):
        settings = RootSettings(
            runtime={"root_dir": "/var/lib/agentloom"},
            logging={
                "level": "DEBUG",
                "console_enabled": False,
                "file_enabled": False,
                "max_file_bytes": 1024,
                "backup_count": 1,
            },
        )
        assert settings.runtime.root_dir == "/var/lib/agentloom"
        assert settings.logging.level == "DEBUG"
        assert settings.logging.file_enabled is False

    @pytest.mark.parametrize("legacy_key", ["enabled", "dir", "file_path"])
    def test_legacy_logging_keys_are_rejected(self, legacy_key: str):
        with pytest.raises(ValueError, match=legacy_key):
            RootSettings(logging={legacy_key: True})

    def test_automatic_runtime_cleanup_cannot_run_more_often_than_daily(self):
        with pytest.raises(ValueError, match="greater than or equal to 24"):
            RootSettings(runtime={"cleanup_interval_hours": 23})

    def test_smart_summary_defaults_true(self):
        settings = RootSettings()
        assert settings.smart_summary is True

    def test_tool_access_control_defaults(self):
        settings = RootSettings()
        assert isinstance(settings.tool_access_control, ToolAccessControlSettings)
        assert settings.tool_access_control.path_validation == []


# ===========================================================================
# Existing fields still work alongside new fields
# ===========================================================================

class TestExistingFieldsCoexistence:
    """Verify existing fields work correctly when new fields are also set."""

    def test_all_fields_together(self):
        """All fields can be set simultaneously without conflicts."""
        settings = RootSettings(
            smart_summary=False,
            model={"provider": "openai"},
            execution_env={"timeout": 60},
            code_agent={"max_steps": 10},
            tools=[{"name": "bash_tool"}],
            context_engine={"min_chars": 2000},
            tool_metadata={"bash_tool": {"label": "Shell"}},
            tool_output_limits={"max_chars": 8000},
            tool_access_control=ToolAccessControlSettings(
                include_paths=["/workspace"],
                path_validation=[],
            ),
        )
        assert settings.smart_summary is False
        assert settings.model["provider"] == "openai"
        assert settings.context_engine["min_chars"] == 2000
        assert settings.tool_metadata["bash_tool"]["label"] == "Shell"
        assert settings.tool_output_limits["max_chars"] == 8000
        assert settings.tool_access_control.include_paths == ["/workspace"]

    def test_system_field_default(self):
        """The system field uses its own default model."""
        settings = RootSettings()
        # system field should have its own defaults from SystemSettings
        assert hasattr(settings, "system")

    def test_extra_fields_allowed(self):
        """RootSettings has extra='allow', so unknown keys don't raise."""
        settings = RootSettings(unknown_future_field="value123")
        assert settings.unknown_future_field == "value123"


# ===========================================================================
# validate_system_snapshot integration
# ===========================================================================

class TestValidateSystemSnapshot:
    """Test that validate_system_snapshot accepts configs with new fields."""

    def test_snapshot_with_tool_metadata(self):
        """Snapshot containing tool_metadata should pass validation."""
        snapshot = {"tool_metadata": {"bash_tool": {"timeout": 30}}}
        # Should not raise
        validate_system_snapshot(snapshot, "test")

    def test_snapshot_with_tool_output_limits(self):
        """Snapshot containing tool_output_limits should pass validation."""
        snapshot = {"tool_output_limits": {"max_chars": 5000}}
        validate_system_snapshot(snapshot, "test")

    def test_snapshot_with_both_new_fields(self):
        """Snapshot with both new fields should pass."""
        snapshot = {
            "context_engine": {"min_chars": 2000},
            "tool_metadata": {"grep": {}},
            "tool_output_limits": {"max_chars": 10000},
        }
        validate_system_snapshot(snapshot, "test")

    def test_snapshot_rejects_project_key(self):
        """The 'project' key is still rejected."""
        snapshot = {"project": {"name": "test"}}
        with pytest.raises(ValueError, match="Unsupported top-level key 'project'"):
            validate_system_snapshot(snapshot, "test")

    def test_snapshot_rejects_removed_tools_mapping(self):
        with pytest.raises(ValueError, match="tools_mapping"):
            validate_system_snapshot(
                {"tools_mapping": {"Claude": {"Read": "read_file"}}},
                "test",
            )

    def test_snapshot_empty_passes(self):
        """An empty snapshot should be valid."""
        validate_system_snapshot({}, "test")

    def test_snapshot_rejects_legacy_self_learning_root(self):
        with pytest.raises(ValueError, match="self_learning.root_dir"):
            validate_system_snapshot(
                {"self_learning": {"root_dir": "/tmp/split-runtime"}},
                "test",
            )


@pytest.mark.parametrize("key", ["runtime", "logging"])
def test_application_config_rejects_global_only_runtime_and_logging(key: str) -> None:
    with pytest.raises(ValueError, match=rf"global-only.*{key}"):
        _reject_application_global_only_keys(
            {
                key: {"root_dir": "other"},
                "checkpoint": {"enabled": False},
            },
            source_name="app/config/system.yaml",
        )


@pytest.mark.parametrize("key", ["runtime", "logging"])
def test_agent_yaml_rejects_global_only_runtime_and_logging(key: str) -> None:
    with pytest.raises(ValueError, match=rf"global-only.*{key}"):
        extract_workflow_overlay(
            {key: {"file_enabled": False}},
            source_name="agent.yaml",
        )
