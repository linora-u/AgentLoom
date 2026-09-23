import logging

import pytest
from agentloom.config.config_validation import (
    BoolParser,
    EnumParser,
    FloatParser,
    IntParser,
    LogLevelParser,
)


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


@pytest.mark.parametrize(
    ("parser", "value", "default"),
    [
        (BoolParser, object(), False),
        (IntParser, object(), 0),
        (FloatParser, object(), 0.0),
        (LogLevelParser, object(), logging.INFO),
    ],
)
def test_parser_warns_through_the_supplied_standard_logger(
    caplog,
    parser,
    value,
    default,
):
    logger = logging.getLogger("agentloom.config.parser-test")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert parser.parse(value, default=default, logger=logger) == default

    assert len(caplog.records) == 1
    assert caplog.records[0].name == logger.name
    assert "defaulting" in caplog.records[0].message


def test_enum_parser_warns_through_the_supplied_standard_logger(caplog):
    logger = logging.getLogger("agentloom.config.enum-parser-test")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert (
            EnumParser.parse(
                "unknown",
                {"KNOWN": "known"},
                default="known",
                logger=logger,
            )
            == "known"
        )

    assert len(caplog.records) == 1
    assert caplog.records[0].name == logger.name
