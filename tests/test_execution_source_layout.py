import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from applications.memory_feature_validation.scripts.run_offline_memory_campaign import (  # noqa: E402
    _source_paths_for_tree,
)


def test_release_manifest_uses_current_execution_owner() -> None:
    paths = _source_paths_for_tree(
        {
            "src/self_learning/event_schema.py",
            "src/execution/__init__.py",
        }
    )

    assert "src/execution/context.py" in paths
    assert "src/execution/trusted_memory_evidence.py" in paths


def test_release_manifest_preserves_historical_runtime_owner() -> None:
    paths = _source_paths_for_tree(
        {
            "src/self_learning/event_schema.py",
            "src/runtime/__init__.py",
        }
    )

    assert "src/runtime/context.py" in paths
    assert "src/runtime/trusted_memory_evidence.py" in paths
