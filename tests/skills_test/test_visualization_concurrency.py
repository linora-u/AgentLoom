import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _load_common():
    path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "agent-visualization"
        / "scripts"
        / "common.py"
    )
    spec = importlib.util.spec_from_file_location("agent_visualization_common", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_concurrent_visualization_events_are_not_lost(tmp_path: Path) -> None:
    common = _load_common()
    path = tmp_path / "visualization.json"
    common.write_viz_state(
        path,
        {"config": {"title": "test", "agents": []}, "timeline": []},
    )

    def append(index: int) -> None:
        common.append_event(
            path,
            agent_name="worker",
            agent_type="worker",
            event_type="tool_call",
            status="working",
            description=str(index),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(80)))

    data = common.read_viz_state(path)
    assert len(data["timeline"]) == 80
    assert sorted(event["step"] for event in data["timeline"]) == list(range(1, 81))
    assert not list(tmp_path.glob("*.tmp"))
