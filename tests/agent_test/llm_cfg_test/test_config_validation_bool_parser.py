import pytest

from src.lib.config.config_validation import BoolParser


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("off", False),
        (" yes ", True),
    ],
)
def test_parse_bool_supports_common_shapes(raw_value, expected):
    assert BoolParser.parse(raw_value) is expected


@pytest.mark.parametrize("raw_value", [None, "maybe", [], object()])
def test_parse_bool_returns_default_for_invalid_shapes(raw_value):
    assert BoolParser.parse(raw_value, default=True) is True
    assert BoolParser.parse(raw_value, default=False) is False
