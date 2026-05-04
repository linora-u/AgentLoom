import importlib.util
from pathlib import Path

import pytest

# === UNIT_TEST_STUDIO GENERATED TEST START ===
MODULE_FILE = (Path(__file__).parent / "../fixtures/sample_project/src/text_pipeline.py").resolve()
FUNCTION_NAME = "extract_keywords"

def _load_function():
    spec = importlib.util.spec_from_file_location(
        "unit_test_studio_target_module", MODULE_FILE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from file: {MODULE_FILE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, FUNCTION_NAME)

TARGET_FUNCTION = _load_function()

TEST_CASES = [
    {
        "name": "baseline_behavior",
        "input": {
            "message": "  Hello, World!  "
        },
        "expected": [
            "hello",
            "world"
        ]
    },
    {
        "name": "edge_behavior",
        "input": {
            "message": ""
        },
        "expected": [
            "empty"
        ]
    },
    {
        "name": "alternate_flags_or_limits",
        "input": {
            "message": "  Hello, World!  ",
            "limit": 3
        },
        "expected": [
            "hello",
            "world"
        ]
    }
]

@pytest.mark.parametrize("case", TEST_CASES, ids=[case["name"] for case in TEST_CASES])
def test_extract_keywords_parameterized(case):
    result = TARGET_FUNCTION(**case["input"])
    assert result == case["expected"]
# === UNIT_TEST_STUDIO GENERATED TEST END ===
